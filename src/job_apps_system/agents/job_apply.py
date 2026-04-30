from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.agents.apply.ai_browser_loop import AiBrowserApplyLoop
from job_apps_system.agents.apply.ashby_adapter import AshbyApplyAdapter
from job_apps_system.agents.apply.ats_detector import ASHBY, DICE, GREENHOUSE, ICIMS, ORACLE_CLOUD, detect_ats_type
from job_apps_system.agents.apply.dice_adapter import DiceApplyAdapter
from job_apps_system.agents.apply.greenhouse_adapter import GreenhouseApplyAdapter
from job_apps_system.agents.apply.icims_adapter import IcimsApplyAdapter
from job_apps_system.agents.apply.oracle_cloud_adapter import OracleCloudApplyAdapter
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.config.settings import settings
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.resumes import ResumeArtifact
from job_apps_system.integrations.google.drive import download_drive_file
from job_apps_system.schemas.apply import ApplyJobResult, JobApplySummary
from job_apps_system.services.application_answer_service import ApplicationAnswerService
from job_apps_system.services.apply_site_sessions import (
    ApplySiteSession,
    build_apply_site_session,
    resolve_apply_profile_path,
)
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
        mode: str = "ai",
        step_reporter=None,
        cancel_checker=None,
    ) -> JobApplySummary:
        if mode == "ai":
            self._validate_applicant_profile(self._config.applicant)
        pending_jobs = self._eligible_jobs(limit=limit, job_ids=job_ids or [], mode=mode)
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
                result = self._apply_to_job(job, mode=mode, cancel_checker=cancel_checker)
                applied_jobs.append(result)
                if result.success:
                    self._record_success(job, result)
                elif result.status == "manual_closed":
                    self._record_manual_close(job, result)
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
            ok=failed_count == 0,
            cancelled=False,
            message="Apply agent completed." if failed_count == 0 else "Apply agent failed.",
            pending_jobs=len(pending_jobs),
            attempted_count=attempted_count,
            applied_count=submitted_count,
            failed_count=failed_count,
            applied_jobs=applied_jobs,
        )

    def _eligible_jobs(self, *, limit: int | None, job_ids: list[str], mode: str) -> list[Job]:
        query = select(Job).where(Job.project_id == self._project_id)
        query = query.where((Job.intake_decision.is_(None)) | (Job.intake_decision == "accepted"))
        query = query.where((Job.applied.is_(False)) | (Job.applied.is_(None)))
        query = query.where(Job.apply_url.is_not(None), Job.apply_url != "")
        if job_ids:
            query = query.where(Job.id.in_(job_ids))
        else:
            query = query.where(Job.score.is_not(None), Job.score >= self._config.app.score_threshold)
        if mode == "ai":
            query = query.where(Job.resume_url.is_not(None), Job.resume_url != "")
        rows = self._session.scalars(query.order_by(Job.created_time.asc().nullslast(), Job.id.asc())).all()

        if mode == "manual":
            filtered = rows
        else:
            filtered = [row for row in rows if self._resolve_resume_ref(row)]
        if limit is not None and limit > 0:
            return filtered[:limit]
        return filtered

    def _apply_to_job(self, job: Job, *, mode: str, cancel_checker=None) -> ApplyJobResult:
        ats_type = detect_ats_type(job.apply_url)
        if mode == "manual":
            return self._manual_apply_to_job(job, ats_type=ats_type, cancel_checker=cancel_checker)

        resume_ref = self._resolve_resume_ref(job)
        if not resume_ref:
            raise RuntimeError("Generated resume PDF was not found for this job.")

        logger.info("Resolved resume reference. job_id=%s resume_ref=%s ats_hint=%s", job.id, resume_ref, ats_type)

        with tempfile.TemporaryDirectory(prefix="job-apply-") as temp_dir:
            resume_path = self._download_resume_pdf(resume_ref, Path(temp_dir))
            screenshot_path = self._screenshot_path(job)
            apply_session = self._apply_site_session_for(job.apply_url, ats_type)
            logger.info(
                "Prepared apply artifacts. job_id=%s resume_path=%s screenshot_path=%s headless=%s auto_submit=%s site_key=%s profile=%s credential=%s",
                job.id,
                resume_path,
                screenshot_path,
                self._config.app.apply_headless,
                self._config.app.apply_auto_submit,
                apply_session.site_key,
                apply_session.profile_path,
                bool(apply_session.credential),
            )
            with sync_playwright() as playwright:
                context = self._launch_apply_context(playwright, apply_session, headless=self._config.app.apply_headless)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    try:
                        page.goto(job.apply_url or "", wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(3000)
                        ats_type = detect_ats_type(job.apply_url, page)
                        logger.info("Detected ATS type after navigation. job_id=%s ats_type=%s page_url=%s", job.id, ats_type, page.url)
                        adapter = self._adapter_for_ats(ats_type)
                        if adapter is None:
                            logger.warning(
                                "Unsupported ATS type; using AI browser apply loop. job_id=%s ats_type=%s apply_url=%s",
                                job.id,
                                ats_type,
                                job.apply_url,
                            )
                            result = self._run_ai_browser_loop(
                                page=page,
                                job=job,
                                ats_type=ats_type,
                                resume_path=resume_path,
                                screenshot_path=screenshot_path,
                                cancel_checker=cancel_checker,
                                site_credential=apply_session.credential,
                                initial_reason=f"Unsupported application page type: {ats_type}.",
                            )
                            return self._attach_discovered_destination(result, context=context, page=page, job=job)
                        adapter_result = adapter.apply(
                            page=page,
                            job=job,
                            applicant=self._config.applicant,
                            resume_path=resume_path,
                            answer_service=self._answer_service,
                            screenshot_path=screenshot_path,
                            auto_submit=self._config.app.apply_auto_submit,
                            cancel_checker=cancel_checker,
                            site_credential=apply_session.credential,
                        )
                        if self._should_recover_with_ai_browser(adapter_result):
                            page = self._active_page(context, page)
                            ats_type = detect_ats_type(page.url if not page.is_closed() else job.apply_url, page if not page.is_closed() else None)
                            logger.warning(
                                "Adapter apply failed; trying AI browser recovery. job_id=%s ats_type=%s status=%s error=%s",
                                job.id,
                                ats_type,
                                adapter_result.status,
                                adapter_result.error,
                            )
                            result = self._run_ai_browser_loop(
                                page=page,
                                job=job,
                                ats_type=ats_type,
                                resume_path=resume_path,
                                screenshot_path=screenshot_path,
                                cancel_checker=cancel_checker,
                                site_credential=apply_session.credential,
                                initial_reason=self._ai_recovery_reason(adapter_result),
                            )
                            return self._attach_discovered_destination(result, context=context, page=page, job=job)
                        return self._attach_discovered_destination(adapter_result, context=context, page=page, job=job)
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
                        result = ApplyJobResult(
                            job_id=job.id,
                            company_name=job.company_name,
                            job_title=job.job_title,
                            ats_type=ats_type,
                            status="failed",
                            success=False,
                            error=str(exc),
                            screenshot_path=captured_path,
                        )
                        return self._attach_discovered_destination(result, context=context, page=page, job=job)
                finally:
                    context.close()

    def _manual_apply_to_job(self, job: Job, *, ats_type: str, cancel_checker=None) -> ApplyJobResult:
        steps: list[str] = []
        logger.info(
            "Opening manual apply browser session. job_id=%s company=%s title=%s apply_url=%s",
            job.id,
            job.company_name,
            job.job_title,
            job.apply_url,
        )
        apply_session = self._apply_site_session_for(job.apply_url, ats_type)
        with sync_playwright() as playwright:
            context = self._launch_apply_context(playwright, apply_session, headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(job.apply_url or "", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                steps.append(f"Opened manual application browser session using profile {apply_session.site_key}.")
                discovered_apply_url = None
                discovered_company_name = None
                while not page.is_closed():
                    if self._is_cancelled(cancel_checker):
                        context.close()
                        raise RuntimeError("Apply agent cancelled.")
                    destination = self._discovered_destination_from_page(page, job)
                    if destination is not None:
                        discovered_apply_url, discovered_company_name = destination
                    page.wait_for_timeout(1000)
                steps.append("Manual application window closed by user.")
                return ApplyJobResult(
                    job_id=job.id,
                    company_name=job.company_name,
                    job_title=job.job_title,
                    ats_type=ats_type,
                    status="manual_closed",
                    success=False,
                    confirmation_text="Manual application window closed.",
                    discovered_apply_url=discovered_apply_url,
                    discovered_company_name=discovered_company_name,
                    steps=steps,
                )
            finally:
                try:
                    context.close()
                except Exception:
                    pass

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
        self._apply_discovered_destination(job, result)
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
        self._apply_discovered_destination(job, result)
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

    def _record_manual_close(self, job: Job, result: ApplyJobResult) -> None:
        self._apply_discovered_destination(job, result)
        manual_message = " ".join(
            part for part in [result.error, result.confirmation_text] if isinstance(part, str) and part.strip()
        )
        normalized = manual_message.lower()
        if "captcha" in normalized or "hcaptcha" in normalized or "recaptcha" in normalized:
            job.application_status = "captcha"
            job.application_error = manual_message or job.application_error
        elif job.application_status != "captcha":
            job.application_status = result.status
            job.application_error = None
        job.application_screenshot_path = result.screenshot_path
        logger.info(
            "Stored manual application session result. job_id=%s status=%s screenshot=%s",
            job.id,
            job.application_status,
            result.screenshot_path,
        )
        self._session.flush()

    def _apply_discovered_destination(self, job: Job, result: ApplyJobResult) -> None:
        apply_url = (result.discovered_apply_url or "").strip()
        company_name = (result.discovered_company_name or "").strip()
        changed = False
        if apply_url and _should_store_discovered_apply_url(apply_url, job.apply_url):
            job.apply_url = apply_url
            changed = True
            company_url, company_domain = _company_site_from_apply_url(apply_url)
            if company_url:
                job.company_url = company_url
            if company_domain:
                job.company_domain = company_domain
        if company_name and _should_store_discovered_company_name(company_name, job.company_name):
            job.company_name = company_name
            changed = True
        if changed:
            logger.info(
                "Updated job from discovered application destination. job_id=%s company=%s apply_url=%s",
                job.id,
                job.company_name,
                job.apply_url,
            )

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

    def _run_ai_browser_loop(
        self,
        *,
        page,
        job: Job,
        ats_type: str,
        resume_path: Path,
        screenshot_path: Path,
        cancel_checker=None,
        site_credential=None,
        initial_reason: str = "",
    ) -> ApplyJobResult:
        loop = AiBrowserApplyLoop(retain_success_logs=self._config.app.apply_debug_retain_success_logs)
        return loop.apply(
            page=page,
            job=job,
            applicant=self._config.applicant,
            resume_path=resume_path,
            answer_service=self._answer_service,
            screenshot_path=screenshot_path,
            auto_submit=self._config.app.apply_auto_submit,
            detected_ats=ats_type,
            site_credential=site_credential,
            initial_reason=initial_reason,
            cancel_checker=cancel_checker,
        )

    def _attach_discovered_destination(self, result: ApplyJobResult, *, context, page, job: Job) -> ApplyJobResult:
        page = self._active_page(context, page)
        destination = self._discovered_destination_from_page(page, job)
        if destination is None:
            return result
        apply_url, company_name = destination
        result.discovered_apply_url = apply_url
        result.discovered_company_name = company_name
        result.steps.append(f"Discovered final application destination: {company_name or 'Unknown company'} — {apply_url}")
        return result

    def _discovered_destination_from_page(self, page, job: Job) -> tuple[str, str | None] | None:
        try:
            if page.is_closed():
                return None
            current_url = page.url
        except Exception:
            return None
        if not _should_store_discovered_apply_url(current_url, job.apply_url):
            return None
        company_name = self._company_name_from_page(page, current_url)
        return current_url, company_name

    def _company_name_from_page(self, page, apply_url: str) -> str | None:
        candidates: list[str] = []
        try:
            meta_value = page.locator(
                "meta[property='og:site_name'], meta[name='application-name'], meta[name='apple-mobile-web-app-title']"
            ).evaluate_all("els => els.map((el) => el.content || '').find(Boolean) || ''")
            if str(meta_value or "").strip():
                candidates.append(str(meta_value).strip())
        except PlaywrightError:
            pass
        candidates.append(_company_name_from_url(apply_url) or "")
        try:
            title = page.title()
            if title:
                candidates.extend(_company_candidates_from_title(title))
        except PlaywrightError:
            pass
        for candidate in candidates:
            cleaned = _clean_company_name(candidate)
            if cleaned and not _is_generic_application_provider_name(cleaned):
                return cleaned
        return None

    def _apply_site_session_for(self, apply_url: str | None, ats_type: str | None) -> ApplySiteSession:
        return build_apply_site_session(
            url=apply_url,
            ats_type=ats_type,
            applicant_email=self._config.applicant.email,
            linkedin_profile_path=self._config.linkedin.browser_profile_path,
            session=self._session,
        )

    @staticmethod
    def _launch_apply_context(playwright, apply_session: ApplySiteSession, *, headless: bool):
        apply_session.profile_path.mkdir(parents=True, exist_ok=True)
        try:
            return playwright.firefox.launch_persistent_context(
                user_data_dir=str(apply_session.profile_path),
                headless=headless,
                viewport={"width": 1280, "height": 900},
            )
        except PlaywrightError as exc:
            message = str(exc)
            if "lock" in message.lower() or "in use" in message.lower():
                if apply_session.site_key == "linkedin":
                    fallback_path = resolve_apply_profile_path("linkedin-apply")
                    fallback_path.mkdir(parents=True, exist_ok=True)
                    logger.warning(
                        "LinkedIn intake profile is locked; using dedicated apply profile. original_profile=%s fallback_profile=%s",
                        apply_session.profile_path,
                        fallback_path,
                    )
                    return playwright.firefox.launch_persistent_context(
                        user_data_dir=str(fallback_path),
                        headless=headless,
                        viewport={"width": 1280, "height": 900},
                    )
                raise RuntimeError(
                    f"Apply browser profile '{apply_session.site_key}' is already open. "
                    "Close the automation browser before running this application."
                ) from exc
            raise

    @staticmethod
    def _active_page(context, fallback):
        for candidate in reversed(context.pages):
            try:
                if not candidate.is_closed():
                    return candidate
            except Exception:
                continue
        return fallback

    @staticmethod
    def _should_recover_with_ai_browser(result: ApplyJobResult) -> bool:
        if result.success:
            return False
        if result.status not in {"failed", "needs_review"}:
            return False
        message = " ".join(
            part for part in [result.error, result.confirmation_text] if isinstance(part, str) and part.strip()
        )
        normalized = message.lower()
        if "unsupported application page type" in normalized:
            return True
        if "apply agent cancelled" in normalized:
            return False
        return True

    @staticmethod
    def _ai_recovery_reason(result: ApplyJobResult) -> str:
        parts = [
            f"deterministic adapter returned {result.status}",
            result.error or result.confirmation_text or "",
        ]
        if result.steps:
            parts.append("last adapter steps: " + " | ".join(result.steps[-4:]))
        return ". ".join(part for part in parts if part)

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
        if ats_type == DICE:
            return DiceApplyAdapter()
        if ats_type == ORACLE_CLOUD:
            return OracleCloudApplyAdapter()
        return None


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "resume.pdf"


def _should_store_discovered_apply_url(candidate_url: str | None, current_url: str | None) -> bool:
    parsed = urlparse(candidate_url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if _same_url_without_fragment(candidate_url, current_url):
        return False
    host = parsed.netloc.lower()
    if _is_redirect_or_job_board_host(host):
        return False
    return True


def _same_url_without_fragment(left: str | None, right: str | None) -> bool:
    left_parsed = urlparse(left or "")
    right_parsed = urlparse(right or "")
    return left_parsed._replace(fragment="").geturl() == right_parsed._replace(fragment="").geturl()


def _is_redirect_or_job_board_host(host: str) -> bool:
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in (
            "appcast.io",
            "dice.com",
            "linkedin.com",
            "indeed.com",
            "ziprecruiter.com",
            "monster.com",
        )
    )


def _company_site_from_apply_url(apply_url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(apply_url or "")
    host = parsed.netloc.lower()
    if not host or _is_generic_application_provider_host(host):
        return None, None
    domain = _registrable_domain(host)
    if not domain:
        return None, None
    return f"{parsed.scheme or 'https'}://www.{domain}", domain


def _company_candidates_from_title(title: str) -> list[str]:
    cleaned = " ".join((title or "").split())
    candidates: list[str] = []
    if not cleaned:
        return candidates
    at_match = re.search(r"\bat\s+([^|–—-]+)", cleaned, re.IGNORECASE)
    if at_match:
        candidates.append(at_match.group(1))
    for separator in (" | ", " - ", " – ", " — "):
        if separator in cleaned:
            parts = [part.strip() for part in cleaned.split(separator) if part.strip()]
            candidates.extend(reversed(parts[-2:]))
    candidates.append(cleaned)
    return candidates


def _company_name_from_url(apply_url: str) -> str | None:
    parsed = urlparse(apply_url or "")
    host = parsed.netloc.lower()
    if not host:
        return None
    if "oraclecloud.com" in host:
        subdomain = host.split(".oraclecloud.com", 1)[0].split(".", 1)[0]
        override = _brand_override(subdomain)
        if override:
            return override
    base = _registrable_domain(host)
    if not base:
        return None
    name_part = base.split(".", 1)[0]
    if _is_generic_application_provider_name(name_part):
        path_parts = [part for part in parsed.path.split("/") if part]
        for part in path_parts:
            if not _is_generic_application_provider_name(part):
                name_part = part
                break
    return _brand_name_from_slug(name_part)


def _registrable_domain(host: str) -> str | None:
    parts = [part for part in host.split(".") if part and part != "www"]
    if len(parts) < 2:
        return None
    return ".".join(parts[-2:])


def _clean_company_name(value: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", value or "").strip(" -–—|")
    override = _brand_override(cleaned)
    if override:
        return override
    cleaned = re.sub(r"\b(careers?|jobs?|job openings?|apply|application|registration|login)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bcandidate experience page\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|")
    if not cleaned or len(cleaned) > 80:
        return None
    if "." in cleaned and " " not in cleaned:
        return _company_name_from_url(f"https://{cleaned}")
    if cleaned.islower() or "-" in cleaned or "_" in cleaned:
        return _brand_name_from_slug(cleaned)
    return _brand_override(cleaned) or cleaned


def _brand_name_from_slug(value: str) -> str | None:
    slug = re.sub(r"[^a-zA-Z0-9]+", " ", value or "").strip().lower()
    if not slug:
        return None
    override = _brand_override(slug)
    if override:
        return override
    return " ".join(part.capitalize() for part in slug.split())


def _brand_override(value: str) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "", (value or "").lower())
    return {
        "github": "GitHub",
        "jpmc": "JPMorganChase",
        "jpmccandidateexperiencepage": "JPMorganChase",
        "jpmorganchase": "JPMorganChase",
        "kforce": "Kforce",
        "roberthalf": "Robert Half",
        "capitalone": "Capital One",
        "bankofamerica": "Bank of America",
        "boozallenhamilton": "Booz Allen Hamilton",
    }.get(key)


def _should_store_discovered_company_name(candidate: str, current: str | None) -> bool:
    cleaned = _clean_company_name(candidate)
    if not cleaned or _is_generic_application_provider_name(cleaned):
        return False
    current_normalized = re.sub(r"[^a-z0-9]+", "", (current or "").lower())
    candidate_normalized = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
    if candidate_normalized and candidate_normalized == current_normalized:
        return False
    if current_normalized in {"jobsviadice", "dice", "linkedin", "jobsvialinkedin", "appcast"}:
        return True
    return bool(candidate_normalized)


def _is_generic_application_provider_host(host: str) -> bool:
    base = (_registrable_domain(host) or host).split(".", 1)[0]
    return _is_generic_application_provider_name(base)


def _is_generic_application_provider_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", (value or "").lower())
    return normalized in {
        "appcast",
        "ashby",
        "ashbyhq",
        "bamboohr",
        "candidateexperiencepage",
        "dice",
        "greenhouse",
        "greenhouseio",
        "icims",
        "indeed",
        "jobvite",
        "lever",
        "linkedin",
        "monster",
        "myworkdayjobs",
        "oraclecloud",
        "smartrecruiters",
        "workable",
        "workday",
        "workdayjobs",
        "ziprecruiter",
    }
