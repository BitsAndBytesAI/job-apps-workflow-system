from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.google.sheets import GoogleSheetsClient
from job_apps_system.integrations.linkedin.scraper import LinkedInScraper
from job_apps_system.schemas.jobs import JobIntakeRunSummary, ScrapedJob
from job_apps_system.services.setup_config import load_setup_config
from job_apps_system.services.sheet_sync import EM_JOBS_HEADERS, SheetSyncService


DIRECTOR_TITLE_TERMS = ("director", "vice president", " vp", "head of")


class JobIntakeAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._sheet_sync = SheetSyncService(session)
        self._sheets = GoogleSheetsClient(session=session)
        self._cancel_checker = None

    def run(
        self,
        search_urls: list[str] | None = None,
        max_jobs_per_search: int = 25,
        step_reporter=None,
        cancel_checker=None,
    ) -> JobIntakeRunSummary:
        self._cancel_checker = cancel_checker
        resolved_search_urls = [url.strip() for url in (search_urls or self._config.linkedin.search_urls) if url.strip()]
        if not resolved_search_urls:
            return JobIntakeRunSummary(ok=False, message="No LinkedIn search URLs are configured.")

        resources = self._config.google.resources
        if not resources.em_jobs_sheet or not resources.processed_jobs_sheet:
            return JobIntakeRunSummary(ok=False, message="Google Sheets are not fully configured for the jobs agent.")

        self._report_step(step_reporter, "Validate configuration", "completed", "Configuration looks valid.")
        cancelled_summary = self._cancelled_summary(
            resolved_search_urls,
            step_reporter,
            "Validate configuration",
            "Cancellation requested. Jobs agent stopped before sheet preparation.",
        )
        if cancelled_summary is not None:
            return cancelled_summary

        self._report_step(step_reporter, "Ensure Google sheet headers", "running", "Checking configured Google sheet headers.")
        self._sheet_sync.ensure_configured_headers()
        self._report_step(step_reporter, "Ensure Google sheet headers", "completed", "Google sheet headers are ready.")
        cancelled_summary = self._cancelled_summary(
            resolved_search_urls,
            step_reporter,
            "Ensure Google sheet headers",
            "Cancellation requested. Jobs agent stopped after preparing sheet headers.",
        )
        if cancelled_summary is not None:
            return cancelled_summary

        self._report_step(step_reporter, "Sync jobs sheet to local DB", "running", "Loading existing job rows into the local database.")
        self._sheet_sync.sync_em_jobs_to_db()
        self._report_step(step_reporter, "Sync jobs sheet to local DB", "completed", "Existing job rows are synced locally.")
        cancelled_summary = self._cancelled_summary(
            resolved_search_urls,
            step_reporter,
            "Sync jobs sheet to local DB",
            "Cancellation requested. Jobs agent stopped after local DB sync.",
        )
        if cancelled_summary is not None:
            return cancelled_summary

        self._report_step(step_reporter, "Scrape LinkedIn", "running", "Opening LinkedIn and collecting job cards.")
        scraper = LinkedInScraper(self._config.linkedin.browser_profile_path)
        scraped_jobs = scraper.scrape_search_urls(resolved_search_urls, max_jobs_per_search=max_jobs_per_search)
        self._report_step(step_reporter, "Scrape LinkedIn", "completed", f"Scraped {len(scraped_jobs)} job(s) from LinkedIn.")
        cancelled_summary = self._cancelled_summary(
            resolved_search_urls,
            step_reporter,
            "Scrape LinkedIn",
            "Cancellation requested. Jobs agent stopped after scraping LinkedIn.",
        )
        if cancelled_summary is not None:
            return cancelled_summary

        processed_ids = self._sheet_sync.get_processed_job_ids()
        existing_em_job_ids = {record["id"] for record in self._sheet_sync.get_em_job_records() if record.get("id")}
        known_ids = processed_ids | existing_em_job_ids | self._existing_job_ids()

        self._report_step(step_reporter, "Analyze scraped jobs", "running", "Scoring, filtering, and deduplicating scraped jobs.")
        accepted_jobs: list[ScrapedJob] = []
        processed_rows: list[dict[str, Any]] = []
        duplicate_count = 0
        filtered_count = 0
        processed_count = 0
        now = datetime.now(timezone.utc)

        for job in scraped_jobs:
            if job.id in known_ids:
                duplicate_count += 1
                continue

            decision = "accepted"
            if self._is_filtered_title(job.job_title):
                decision = "filtered_title"

            processed_rows.append(self._build_processed_row(job, decision=decision, processed_at=now))
            self._upsert_job(job, created_at=now)
            processed_count += 1

            if decision == "accepted":
                accepted_jobs.append(job)
                known_ids.add(job.id)
            else:
                filtered_count += 1

        self._report_step(
            step_reporter,
            "Analyze scraped jobs",
            "completed",
            f"Accepted {len(accepted_jobs)}, duplicates {duplicate_count}, filtered {filtered_count}.",
        )
        cancelled_summary = self._cancelled_summary(
            resolved_search_urls,
            step_reporter,
            "Analyze scraped jobs",
            "Cancellation requested. Jobs agent stopped after analysis.",
        )
        if cancelled_summary is not None:
            return cancelled_summary

        if accepted_jobs:
            self._report_step(
                step_reporter,
                "Resolve apply URLs",
                "running",
                "Inspecting accepted LinkedIn job detail pages for apply destinations.",
            )
            apply_summary = scraper.resolve_apply_urls(accepted_jobs)
            self._report_step(
                step_reporter,
                "Resolve apply URLs",
                "completed",
                "Resolved apply URLs for "
                f"{len(accepted_jobs)} accepted job(s): "
                f"{apply_summary['resolved_external']} external, "
                f"{apply_summary['easy_apply']} easy apply, "
                f"{apply_summary['detail_page']} LinkedIn detail page.",
            )
            self._update_apply_urls(accepted_jobs)
            cancelled_summary = self._cancelled_summary(
                resolved_search_urls,
                step_reporter,
                "Resolve apply URLs",
                "Cancellation requested. Jobs agent stopped after apply URL resolution.",
            )
            if cancelled_summary is not None:
                return cancelled_summary
        else:
            self._report_step(
                step_reporter,
                "Resolve apply URLs",
                "completed",
                "No accepted jobs required apply URL resolution.",
            )

        if not self._config.app.dry_run:
            if accepted_jobs:
                self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "running", "Writing accepted jobs to the jobs sheet.")
                em_rows = [self._build_em_job_row(job, created_at=now) for job in accepted_jobs]
                self._sheet_sync.append_records(resources.em_jobs_sheet, EM_JOBS_HEADERS, em_rows)
                self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "completed", f"Wrote {len(em_rows)} accepted job row(s).")
                cancelled_summary = self._cancelled_summary(
                    resolved_search_urls,
                    step_reporter,
                    "Append accepted jobs to jobs sheet",
                    "Cancellation requested. Jobs agent stopped after writing accepted jobs.",
                )
                if cancelled_summary is not None:
                    return cancelled_summary

                self._report_step(step_reporter, "Resync jobs sheet to local DB", "running", "Refreshing the local DB from the jobs sheet.")
                self._sheet_sync.sync_em_jobs_to_db()
                self._report_step(step_reporter, "Resync jobs sheet to local DB", "completed", "Local DB refreshed from the jobs sheet.")
                cancelled_summary = self._cancelled_summary(
                    resolved_search_urls,
                    step_reporter,
                    "Resync jobs sheet to local DB",
                    "Cancellation requested. Jobs agent stopped after jobs sheet DB resync.",
                )
                if cancelled_summary is not None:
                    return cancelled_summary
            else:
                self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "completed", "No accepted jobs to write.")
                self._report_step(step_reporter, "Resync jobs sheet to local DB", "completed", "No jobs sheet resync was needed.")

            if processed_rows:
                self._report_step(step_reporter, "Append processed jobs sheet", "running", "Writing processed job rows.")
                processed_headers = self._sheets.get_header_row(resources.processed_jobs_sheet)
                if processed_headers:
                    self._sheet_sync.append_records(resources.processed_jobs_sheet, processed_headers, processed_rows)
                self._report_step(step_reporter, "Append processed jobs sheet", "completed", f"Wrote {len(processed_rows)} processed job row(s).")
                cancelled_summary = self._cancelled_summary(
                    resolved_search_urls,
                    step_reporter,
                    "Append processed jobs sheet",
                    "Cancellation requested. Jobs agent stopped after processed jobs write.",
                )
                if cancelled_summary is not None:
                    return cancelled_summary
            else:
                self._report_step(step_reporter, "Append processed jobs sheet", "completed", "No processed rows were written.")
        else:
            self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "completed", "Dry run enabled. Skipped jobs sheet write.")
            self._report_step(step_reporter, "Append processed jobs sheet", "completed", "Dry run enabled. Skipped processed sheet write.")
            self._report_step(step_reporter, "Resync jobs sheet to local DB", "completed", "Dry run enabled. Skipped jobs sheet DB resync.")

        return JobIntakeRunSummary(
            ok=True,
            cancelled=False,
            message="Job intake completed.",
            search_urls=resolved_search_urls,
            scraped_count=len(scraped_jobs),
            accepted_count=len(accepted_jobs),
            processed_count=processed_count,
            duplicate_count=duplicate_count,
            filtered_count=filtered_count,
            accepted_jobs=accepted_jobs,
        )

    def _cancelled_summary(
        self,
        search_urls: list[str],
        step_reporter,
        step_name: str,
        message: str,
    ) -> JobIntakeRunSummary | None:
        if not self._is_cancelled():
            return None
        self._report_step(step_reporter, step_name, "completed", message)
        return JobIntakeRunSummary(
            ok=False,
            cancelled=True,
            message="Jobs agent cancelled.",
            search_urls=search_urls,
        )

    def _existing_job_ids(self) -> set[str]:
        rows = self._session.query(Job.id).filter(Job.project_id == self._project_id).all()
        return {row[0] for row in rows if row[0]}

    def _update_apply_urls(self, jobs: list[ScrapedJob]) -> None:
        for job in jobs:
            record = self._session.get(Job, self._record_id(job.id))
            if record is not None:
                record.apply_url = job.apply_url
        self._session.flush()

    def _upsert_job(self, job: ScrapedJob, created_at: datetime) -> None:
        record = self._session.get(Job, self._record_id(job.id))
        if record is None:
            record = Job(record_id=self._record_id(job.id), project_id=self._project_id, id=job.id, applied=False)
            self._session.add(record)

        record.tracking_id = job.tracking_id
        record.company_name = job.company_name
        record.job_title = job.job_title
        record.job_description = job.job_description
        record.posted_date = job.posted_date
        record.job_posting_url = job.job_posting_url
        record.apply_url = job.apply_url
        record.company_url = job.company_url
        record.score = None
        record.created_time = created_at
        self._session.flush()

    def _build_em_job_row(self, job: ScrapedJob, created_at: datetime) -> dict[str, str]:
        return {
            "Applied": "N",
            "Resume URL": "",
            "Posted Date": job.posted_date or "",
            "Score": "",
            "id": job.id,
            "trackingid": job.tracking_id or "",
            "Company Name": job.company_name,
            "Job TItle": job.job_title,
            "Job Description": job.job_description,
            "Apply URL": job.apply_url or "",
            "Company URL": job.company_url or "",
            "Job Posting URL": job.job_posting_url or "",
            "Job Poster": "",
            "Job Poster Title": "",
            "Job Poster LinkedIn": "",
            "Job Poster Email": "",
            "Job Poster Email Sent": "N",
            "CTO": "",
            "CTO Title": "",
            "CTO Email": "",
            "CTO Email Sent": "N",
            "HR": "",
            "HR Title ": "",
            "HR Email": "",
            "HR Email Sent": "N",
        }

    def _build_processed_row(
        self,
        job: ScrapedJob,
        *,
        decision: str,
        processed_at: datetime,
    ) -> dict[str, str]:
        return {
            "id": job.id,
            "trackingid": job.tracking_id or "",
            "Company Name": job.company_name,
            "Job Title": job.job_title,
            "Job TItle": job.job_title,
            "Processed At": processed_at.isoformat(),
            "Created Time": processed_at.isoformat(),
            "Decision": decision,
        }

    def _is_filtered_title(self, title: str) -> bool:
        title_lower = title.lower()
        return any(term in title_lower for term in DIRECTOR_TITLE_TERMS)

    @staticmethod
    def _report_step(step_reporter, name: str, status: str, message: str) -> None:
        if step_reporter is not None:
            step_reporter(name=name, status=status, message=message)

    def _record_id(self, job_id: str) -> str:
        return f"{self._project_id}:{job_id}"

    def _is_cancelled(self) -> bool:
        return bool(self._cancel_checker is not None and self._cancel_checker())
