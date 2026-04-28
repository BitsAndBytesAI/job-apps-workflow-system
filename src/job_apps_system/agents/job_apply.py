from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.agents.apply.ashby_adapter import AshbyApplyAdapter
from job_apps_system.agents.apply.ats_detector import ASHBY, GREENHOUSE, ICIMS, detect_ats_type
from job_apps_system.agents.apply.greenhouse_adapter import GreenhouseApplyAdapter
from job_apps_system.agents.apply.icims_adapter import IcimsApplyAdapter
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.config.settings import settings
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.resumes import ResumeArtifact
from job_apps_system.integrations.google.drive import download_drive_file
from job_apps_system.schemas.apply import ApplyJobResult, JobApplySummary
from job_apps_system.services.application_answer_service import ApplicationAnswerService
from job_apps_system.services.setup_config import load_setup_config


logger = logging.getLogger(__name__)


class JobApplyAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._answer_service = ApplicationAnswerService(session)

    def run(
        self,
        *,
        limit: int | None = 1,
        job_ids: list[str] | None = None,
        step_reporter=None,
        cancel_checker=None,
    ) -> JobApplySummary:
        self._validate_applicant_profile(self._config.applicant)
        pending_jobs = self._eligible_jobs(limit=limit, job_ids=job_ids or [])
        self._report_step(
            step_reporter,
            "Select application job",
            "completed",
            f"Found {len(pending_jobs)} application job(s) ready to submit.",
        )

        if not pending_jobs:
            return JobApplySummary(
                ok=True,
                cancelled=False,
                message="No application jobs are ready to submit.",
                pending_jobs=0,
            )

        applied_jobs: list[ApplyJobResult] = []
        failed_count = 0
        attempted_count = 0

        for job in pending_jobs:
            if self._is_cancelled(cancel_checker):
                return JobApplySummary(
                    ok=False,
                    cancelled=True,
                    message="Apply agent cancelled.",
                    pending_jobs=len(pending_jobs),
                    attempted_count=attempted_count,
                    applied_count=len([item for item in applied_jobs if item.success]),
                    failed_count=failed_count,
                    applied_jobs=applied_jobs,
                )

            attempted_count += 1
            logger.info(
                "Starting application submission. job_id=%s company=%s title=%s apply_url=%s",
                job.id,
                job.company_name,
                job.job_title,
                job.apply_url,
            )
            self._report_step(
                step_reporter,
                "Submit application",
                "running",
                f"Submitting {attempted_count}/{len(pending_jobs)}: {job.company_name or 'Unknown company'} — {job.job_title or 'Untitled role'}.",
            )
            try:
                result = self._apply_to_job(job, cancel_checker=cancel_checker)
                applied_jobs.append(result)
                if result.success:
                    self._record_success(job, result)
                else:
                    failed_count += 1
                    self._record_failure(job, result)
                self._session.commit()
                logger.info(
                    "Application attempt finished. job_id=%s status=%s success=%s screenshot=%s confirmation=%s error=%s steps=%s",
                    job.id,
                    result.status,
                    result.success,
                    result.screenshot_path,
                    result.confirmation_text,
                    result.error,
                    " | ".join(result.steps),
                )
                self._report_step(
                    step_reporter,
                    "Submit application",
                    "running",
                    f"{result.status}: {job.company_name or 'Unknown company'} — {job.job_title or 'Untitled role'}.",
                )
            except Exception as exc:
                self._session.rollback()
                if self._is_cancelled(cancel_checker):
                    return JobApplySummary(
                        ok=False,
                        cancelled=True,
                        message="Apply agent cancelled.",
                        pending_jobs=len(pending_jobs),
                        attempted_count=attempted_count,
                        applied_count=len([item for item in applied_jobs if item.success]),
                        failed_count=failed_count,
                        applied_jobs=applied_jobs,
                    )

                failed_count += 1
                logger.exception(
                    "Application attempt crashed. job_id=%s company=%s title=%s apply_url=%s",
                    job.id,
                    job.company_name,
                    job.job_title,
                    job.apply_url,
                )
                error_result = ApplyJobResult(
                    job_id=job.id,
                    company_name=job.company_name,
                    job_title=job.job_title,
                    ats_type=detect_ats_type(job.apply_url),
                    status="failed",
                    success=False,
                    error=str(exc),
                )
                applied_jobs.append(error_result)
                self._record_failure(job, error_result)
                self._session.commit()
                self._report_step(
                    step_reporter,
                    "Submit application",
                    "running",
                    f"Failed {attempted_count}/{len(pending_jobs)}: {job.company_name or 'Unknown company'} — {exc}",
                )

        submitted_count = len([item for item in applied_jobs if item.success])
        self._report_step(
            step_reporter,
            "Submit application",
            "completed",
            f"Submitted {submitted_count} application(s); {failed_count} failed.",
        )

        return JobApplySummary(
            ok=submitted_count > 0 and failed_count == 0,
            cancelled=False,
            message="Apply agent completed." if submitted_count else "Apply agent failed.",
            pending_jobs=len(pending_jobs),
            attempted_count=attempted_count,
            applied_count=submitted_count,
            failed_count=failed_count,
            applied_jobs=applied_jobs,
        )

    def _eligible_jobs(self, *, limit: int | None, job_ids: list[str]) -> list[Job]:
        query = select(Job).where(Job.project_id == self._project_id)
        query = query.where((Job.intake_decision.is_(None)) | (Job.intake_decision == "accepted"))
        query = query.where((Job.applied.is_(False)) | (Job.applied.is_(None)))
        query = query.where(Job.apply_url.is_not(None), Job.apply_url != "")
        query = query.where(Job.resume_url.is_not(None), Job.resume_url != "")
        query = query.where(Job.score.is_not(None), Job.score >= self._config.app.score_threshold)
        if job_ids:
            query = query.where(Job.id.in_(job_ids))
        rows = self._session.scalars(query.order_by(Job.created_time.asc().nullslast(), Job.id.asc())).all()

        filtered = [row for row in rows if self._resolve_resume_ref(row)]
        if limit is not None and limit > 0:
            return filtered[:limit]
        return filtered

    def _apply_to_job(self, job: Job, *, cancel_checker=None) -> ApplyJobResult:
        resume_ref = self._resolve_resume_ref(job)
        if not resume_ref:
            raise RuntimeError("Generated resume PDF was not found for this job.")

        ats_type = detect_ats_type(job.apply_url)
        logger.info("Resolved resume reference. job_id=%s resume_ref=%s ats_hint=%s", job.id, resume_ref, ats_type)

        with tempfile.TemporaryDirectory(prefix="job-apply-") as temp_dir:
            resume_path = self._download_resume_pdf(resume_ref, Path(temp_dir))
            screenshot_path = self._screenshot_path(job)
            logger.info(
                "Prepared apply artifacts. job_id=%s resume_path=%s screenshot_path=%s headless=%s auto_submit=%s",
                job.id,
                resume_path,
                screenshot_path,
                self._config.app.apply_headless,
                self._config.app.apply_auto_submit,
            )
            with sync_playwright() as playwright:
                browser = playwright.firefox.launch(headless=self._config.app.apply_headless)
                try:
                    context = browser.new_context(viewport={"width": 1280, "height": 900})
                    page = context.new_page()
                    try:
                        page.goto(job.apply_url or "", wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(3000)
                        ats_type = detect_ats_type(job.apply_url, page)
                        logger.info("Detected ATS type after navigation. job_id=%s ats_type=%s page_url=%s", job.id, ats_type, page.url)
                        adapter = self._adapter_for_ats(ats_type)
                        if adapter is None:
                            logger.warning(
                                "Unsupported ATS type for application. job_id=%s ats_type=%s apply_url=%s",
                                job.id,
                                ats_type,
                                job.apply_url,
                            )
                            return ApplyJobResult(
                                job_id=job.id,
                                company_name=job.company_name,
                                job_title=job.job_title,
                                ats_type=ats_type,
                                status="failed",
                                success=False,
                                error=f"Unsupported application page type: {ats_type}. Supported ATS types: ashby, greenhouse, icims.",
                            )
                        return adapter.apply(
                            page=page,
                            job=job,
                            applicant=self._config.applicant,
                            resume_path=resume_path,
                            answer_service=self._answer_service,
                            screenshot_path=screenshot_path,
                            auto_submit=self._config.app.apply_auto_submit,
                            cancel_checker=cancel_checker,
                        )
                    except Exception as exc:
                        if self._is_cancelled(cancel_checker):
                            raise
                        try:
                            page.screenshot(path=str(screenshot_path), full_page=True)
                            captured_path = str(screenshot_path)
                        except Exception:
                            captured_path = None
                        logger.exception(
                            "Application execution failed after browser navigation. job_id=%s ats_type=%s screenshot=%s",
                            job.id,
                            ats_type,
                            captured_path,
                        )
                        return ApplyJobResult(
                            job_id=job.id,
                            company_name=job.company_name,
                            job_title=job.job_title,
                            ats_type=ats_type,
                            status="failed",
                            success=False,
                            error=str(exc),
                            screenshot_path=captured_path,
                        )
                finally:
                    browser.close()

    def _download_resume_pdf(self, resume_ref: str, temp_dir: Path) -> Path:
        content, metadata = download_drive_file(resume_ref, session=self._session)
        name = _safe_filename(metadata.get("name") or "resume.pdf")
        if not name.lower().endswith(".pdf"):
            name = f"{name}.pdf"
        path = temp_dir / name
        path.write_bytes(content)
        return path

    def _resolve_resume_ref(self, job: Job) -> str | None:
        artifact = self._session.get(ResumeArtifact, job.record_id)
        if artifact is None:
            artifact = self._session.scalar(
                select(ResumeArtifact).where(
                    ResumeArtifact.project_id == self._project_id,
                    ResumeArtifact.job_id == job.id,
                )
            )
        if artifact is not None:
            return artifact.pdf_drive_file_id or artifact.pdf_drive_url or job.resume_url
        return job.resume_url

    def _record_success(self, job: Job, result: ApplyJobResult) -> None:
        now = datetime.now(timezone.utc)
        job.applied = True
        job.applied_at = now
        job.application_status = result.status
        job.application_error = None
        job.application_screenshot_path = result.screenshot_path
        logger.info(
            "Stored successful application result. job_id=%s applied_at=%s screenshot=%s",
            job.id,
            now.isoformat(),
            result.screenshot_path,
        )
        self._session.flush()

    def _record_failure(self, job: Job, result: ApplyJobResult) -> None:
        job.application_status = self._failure_status(result)
        job.application_error = result.error or result.confirmation_text
        job.application_screenshot_path = result.screenshot_path
        logger.warning(
            "Stored failed application result. job_id=%s status=%s error=%s screenshot=%s",
            job.id,
            result.status,
            result.error or result.confirmation_text,
            result.screenshot_path,
        )
        self._session.flush()

    @staticmethod
    def _failure_status(result: ApplyJobResult) -> str:
        if result.status != "failed":
            return result.status
        message = " ".join(
            part for part in [result.error, result.confirmation_text] if isinstance(part, str) and part.strip()
        )
        normalized = message.lower()
        if "captcha" in normalized or "hcaptcha" in normalized or "recaptcha" in normalized:
            return "captcha"
        return result.status

    def _screenshot_path(self, job: Job) -> Path:
        directory = settings.resolved_app_data_dir / "debug" / "applications" / self._project_id
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return directory / f"{_safe_filename(job.id)}-{timestamp}.png"

    def _validate_applicant_profile(self, applicant: ApplicantProfileConfig) -> None:
        missing = []
        required = {
            "Legal Name": applicant.legal_name,
            "Email": applicant.email,
            "Phone": applicant.phone,
            "LinkedIn URL": applicant.linkedin_url,
            "City": applicant.city,
            "State": applicant.state,
            "Country": applicant.country,
            "Compensation Expectation": applicant.compensation_expectation,
        }
        for label, value in required.items():
            if not str(value or "").strip():
                missing.append(label)
        if missing:
            raise ValueError(f"Applicant profile is missing required field(s): {', '.join(missing)}.")

    @staticmethod
    def _report_step(step_reporter, name: str, status: str, message: str) -> None:
        if step_reporter is not None:
            step_reporter(name=name, status=status, message=message)

    @staticmethod
    def _is_cancelled(cancel_checker) -> bool:
        return bool(cancel_checker is not None and cancel_checker())

    @staticmethod
    def _adapter_for_ats(ats_type: str):
        if ats_type == ASHBY:
            return AshbyApplyAdapter()
        if ats_type == GREENHOUSE:
            return GreenhouseApplyAdapter()
        if ats_type == ICIMS:
            return IcimsApplyAdapter()
        return None


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "resume.pdf"
