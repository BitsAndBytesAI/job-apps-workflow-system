from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.linkedin.scraper import LinkedInScraper
from job_apps_system.schemas.jobs import JobIntakeRunSummary, ScrapedJob
from job_apps_system.services.setup_config import load_setup_config


DIRECTOR_TITLE_TERMS = ("director", "vice president", " vp", "head of")
ACCEPTED_DECISION = "accepted"
FILTERED_TITLE_DECISION = "filtered_title"


class JobIntakeAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._cancel_checker = None

    def run(
        self,
        search_urls: list[str] | None = None,
        max_jobs_per_search: int | None = None,
        step_reporter=None,
        cancel_checker=None,
    ) -> JobIntakeRunSummary:
        self._cancel_checker = cancel_checker
        resolved_search_urls = [url.strip() for url in (search_urls or self._config.linkedin.search_urls) if url.strip()]
        if not resolved_search_urls:
            return JobIntakeRunSummary(ok=False, message="No LinkedIn search URLs are configured.")

        self._report_step(step_reporter, "Validate configuration", "completed", "Configuration looks valid.")
        cancelled_summary = self._cancelled_summary(
            resolved_search_urls,
            step_reporter,
            "Validate configuration",
            "Cancellation requested. Jobs agent stopped before scraping LinkedIn.",
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

        known_ids = self._existing_job_ids()

        self._report_step(step_reporter, "Analyze scraped jobs", "running", "Scoring, filtering, and deduplicating scraped jobs.")
        accepted_jobs: list[ScrapedJob] = []
        duplicate_count = 0
        filtered_count = 0
        processed_count = 0
        now = datetime.now(timezone.utc)

        for job in scraped_jobs:
            if job.id in known_ids:
                duplicate_count += 1
                continue

            decision = ACCEPTED_DECISION
            if self._is_filtered_title(job.job_title):
                decision = FILTERED_TITLE_DECISION

            if not self._config.app.dry_run:
                self._upsert_job(job, created_at=now, intake_decision=decision)
            processed_count += 1
            known_ids.add(job.id)

            if decision == ACCEPTED_DECISION:
                accepted_jobs.append(job)
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

        if self._config.app.dry_run:
            self._report_step(step_reporter, "Persist jobs in database", "completed", "Dry run enabled. Skipped job persistence.")
        else:
            self._report_step(
                step_reporter,
                "Persist jobs in database",
                "completed",
                f"Saved {processed_count} new job row(s) in the database.",
            )

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
                record.job_posting_url = job.job_posting_url
                record.company_url = job.company_url
        self._session.flush()

    def _upsert_job(self, job: ScrapedJob, *, created_at: datetime, intake_decision: str) -> None:
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
        record.intake_decision = intake_decision
        record.score = None
        record.created_time = created_at
        self._session.flush()

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
