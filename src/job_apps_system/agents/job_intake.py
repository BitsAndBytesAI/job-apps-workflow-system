from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy.orm import Session

from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.google.docs import GoogleDocsClient
from job_apps_system.integrations.google.sheets import GoogleSheetsClient
from job_apps_system.integrations.linkedin.scraper import LinkedInScraper
from job_apps_system.schemas.jobs import JobIntakeRunSummary, ScrapedJob
from job_apps_system.services.setup_config import load_setup_config
from job_apps_system.services.sheet_sync import EM_JOBS_HEADERS, SheetSyncService


DIRECTOR_TITLE_TERMS = ("director", "vice president", " vp", "head of")
STOP_WORDS = {
    "about",
    "across",
    "after",
    "also",
    "and",
    "are",
    "been",
    "building",
    "company",
    "customer",
    "data",
    "engineering",
    "experience",
    "for",
    "from",
    "have",
    "into",
    "job",
    "jobs",
    "lead",
    "manager",
    "need",
    "our",
    "platform",
    "role",
    "team",
    "that",
    "the",
    "their",
    "this",
    "with",
    "work",
    "years",
}


class JobIntakeAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._sheet_sync = SheetSyncService(session)
        self._sheets = GoogleSheetsClient(session=session)

    def run(
        self,
        search_urls: list[str] | None = None,
        max_jobs_per_search: int = 25,
        step_reporter=None,
    ) -> JobIntakeRunSummary:
        resolved_search_urls = [url.strip() for url in (search_urls or self._config.linkedin.search_urls) if url.strip()]
        if not resolved_search_urls:
            return JobIntakeRunSummary(ok=False, message="No LinkedIn search URLs are configured.")

        resources = self._config.google.resources
        if not resources.em_jobs_sheet or not resources.processed_jobs_sheet:
            return JobIntakeRunSummary(ok=False, message="Google Sheets are not fully configured for the jobs agent.")
        if not resources.base_resume_doc:
            return JobIntakeRunSummary(ok=False, message="Base resume doc is not configured.")

        self._report_step(step_reporter, "Validate configuration", "completed", "Configuration looks valid.")

        self._report_step(step_reporter, "Ensure Google sheet headers", "running", "Checking configured Google sheet headers.")
        self._sheet_sync.ensure_configured_headers()
        self._report_step(step_reporter, "Ensure Google sheet headers", "completed", "Google sheet headers are ready.")

        self._report_step(step_reporter, "Sync jobs sheet to local DB", "running", "Loading existing job rows into the local database.")
        self._sheet_sync.sync_em_jobs_to_db()
        self._report_step(step_reporter, "Sync jobs sheet to local DB", "completed", "Existing job rows are synced locally.")

        self._report_step(step_reporter, "Load base resume", "running", "Reading the base resume from Google Docs.")
        resume_text = GoogleDocsClient(session=self._session).get_document_text(resources.base_resume_doc)
        self._report_step(step_reporter, "Load base resume", "completed", "Base resume loaded.")

        self._report_step(step_reporter, "Scrape LinkedIn", "running", "Opening LinkedIn and collecting job cards.")
        scraper = LinkedInScraper(self._config.linkedin.browser_profile_path)
        scraped_jobs = scraper.scrape_search_urls(resolved_search_urls, max_jobs_per_search=max_jobs_per_search)
        self._report_step(step_reporter, "Scrape LinkedIn", "completed", f"Scraped {len(scraped_jobs)} job(s) from LinkedIn.")

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
            score = self._score_job(job, resume_text)
            if self._is_filtered_title(job.job_title):
                decision = "filtered_title"
            elif score < self._config.app.score_threshold:
                decision = "filtered_low_score"

            processed_rows.append(self._build_processed_row(job, decision=decision, score=score, processed_at=now))
            self._upsert_job(job, score=score, created_at=now)
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

        if not self._config.app.dry_run:
            if accepted_jobs:
                self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "running", "Writing accepted jobs to the jobs sheet.")
                em_rows = [self._build_em_job_row(job, resume_text=resume_text, created_at=now) for job in accepted_jobs]
                self._sheet_sync.append_records(resources.em_jobs_sheet, EM_JOBS_HEADERS, em_rows)
                self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "completed", f"Wrote {len(em_rows)} accepted job row(s).")

                self._report_step(step_reporter, "Resync jobs sheet to local DB", "running", "Refreshing the local DB from the jobs sheet.")
                self._sheet_sync.sync_em_jobs_to_db()
                self._report_step(step_reporter, "Resync jobs sheet to local DB", "completed", "Local DB refreshed from the jobs sheet.")
            else:
                self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "completed", "No accepted jobs to write.")
                self._report_step(step_reporter, "Resync jobs sheet to local DB", "completed", "No jobs sheet resync was needed.")

            if processed_rows:
                self._report_step(step_reporter, "Append processed jobs sheet", "running", "Writing processed job rows.")
                processed_headers = self._sheets.get_header_row(resources.processed_jobs_sheet)
                if processed_headers:
                    self._sheet_sync.append_records(resources.processed_jobs_sheet, processed_headers, processed_rows)
                self._report_step(step_reporter, "Append processed jobs sheet", "completed", f"Wrote {len(processed_rows)} processed job row(s).")
            else:
                self._report_step(step_reporter, "Append processed jobs sheet", "completed", "No processed rows were written.")
        else:
            self._report_step(step_reporter, "Append accepted jobs to jobs sheet", "completed", "Dry run enabled. Skipped jobs sheet write.")
            self._report_step(step_reporter, "Append processed jobs sheet", "completed", "Dry run enabled. Skipped processed sheet write.")
            self._report_step(step_reporter, "Resync jobs sheet to local DB", "completed", "Dry run enabled. Skipped jobs sheet DB resync.")

        return JobIntakeRunSummary(
            ok=True,
            message="Job intake completed.",
            search_urls=resolved_search_urls,
            scraped_count=len(scraped_jobs),
            accepted_count=len(accepted_jobs),
            processed_count=processed_count,
            duplicate_count=duplicate_count,
            filtered_count=filtered_count,
            accepted_jobs=accepted_jobs,
        )

    def _existing_job_ids(self) -> set[str]:
        rows = self._session.query(Job.id).filter(Job.project_id == self._project_id).all()
        return {row[0] for row in rows if row[0]}

    def _upsert_job(self, job: ScrapedJob, score: int, created_at: datetime) -> None:
        record = self._session.get(Job, self._record_id(job.id))
        if record is None:
            record = Job(record_id=self._record_id(job.id), project_id=self._project_id, id=job.id, applied=False)
            self._session.add(record)

        record.tracking_id = job.tracking_id
        record.company_name = job.company_name
        record.job_title = job.job_title
        record.job_description = job.job_description
        record.apply_url = job.apply_url
        record.company_url = job.company_url
        record.score = score
        record.created_time = created_at
        self._session.flush()

    def _build_em_job_row(self, job: ScrapedJob, resume_text: str, created_at: datetime) -> dict[str, str]:
        score = self._score_job(job, resume_text)
        return {
            "Applied": "N",
            "Resume URL": "",
            "Created Time": created_at.isoformat(),
            "Score": str(score),
            "id": job.id,
            "trackingid": job.tracking_id or "",
            "Company Name": job.company_name,
            "Job TItle": job.job_title,
            "Job Description": job.job_description,
            "Apply URL": job.apply_url or "",
            "Company URL": job.company_url or "",
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
        score: int,
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
            "Score": str(score),
        }

    def _is_filtered_title(self, title: str) -> bool:
        title_lower = title.lower()
        return any(term in title_lower for term in DIRECTOR_TITLE_TERMS)

    def _score_job(self, job: ScrapedJob, resume_text: str) -> int:
        resume_terms = self._extract_keywords(resume_text, limit=80)
        job_text = f"{job.job_title} {job.job_description} {job.company_name}"
        job_terms = self._extract_keywords(job_text, limit=80)
        shared_terms = resume_terms & job_terms
        overlap_score = min(35, len(shared_terms) * 3)

        title_lower = job.job_title.lower()
        description_lower = job.job_description.lower()
        score = 40 + overlap_score

        if "manager" in title_lower:
            score += 15
        if "engineering" in title_lower or "software" in title_lower:
            score += 10
        if any(term in description_lower for term in ["leadership", "people management", "stakeholder", "cross-functional"]):
            score += 10
        if any(term in description_lower for term in ["aws", "cloud", "distributed systems", "microservices", "platform"]):
            score += 10
        if any(term in description_lower for term in ["react", "node", "typescript", "data"]):
            score += 5

        if self._is_filtered_title(job.job_title):
            score -= 25

        return max(0, min(99, score))

    def _extract_keywords(self, text: str, limit: int) -> set[str]:
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9+#./-]{2,}", text.lower())
        counts = Counter(token for token in tokens if token not in STOP_WORDS)
        return {token for token, _ in counts.most_common(limit)}

    @staticmethod
    def _report_step(step_reporter, name: str, status: str, message: str) -> None:
        if step_reporter is not None:
            step_reporter(name=name, status=status, message=message)

    def _record_id(self, job_id: str) -> str:
        return f"{self._project_id}:{job_id}"
