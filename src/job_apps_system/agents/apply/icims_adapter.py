from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from job_apps_system.agents.apply.action_map import extract_apply_fields, normalized_text
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.schemas.apply import ApplyField, ApplyJobResult
from job_apps_system.services.application_answer_service import ApplicationAnswerService


logger = logging.getLogger(__name__)

SUCCESS_TERMS = (
    "thank you for applying",
    "application submitted",
    "your application has been submitted",
    "we have received your application",
    "application has been received",
)


class IcimsApplyAdapter:
    ats_type = "icims"

    def apply(
        self,
        *,
        page: Page,
        job: Job,
        applicant: ApplicantProfileConfig,
        resume_path: Path,
        answer_service: ApplicationAnswerService,
        screenshot_path: Path,
        auto_submit: bool,
        cancel_checker=None,
    ) -> ApplyJobResult:
        steps: list[str] = []
        logger.info(
            "iCIMS apply started. job_id=%s company=%s title=%s page_url=%s",
            job.id,
            job.company_name,
            job.job_title,
            page.url,
        )
        try:
            self._check_cancelled(cancel_checker)
            frame = self._resolve_icims_frame(page)
            self._record_step(steps, "Resolved iCIMS application frame.")

            frame = self._advance_email_gate(page, frame, applicant, cancel_checker, steps)
            self._record_step(steps, "Advanced past the iCIMS email gate.")
            self._check_cancelled(cancel_checker)

            if self._has_password_gate(frame):
                raise RuntimeError("iCIMS requires an existing account password for this application.")

            fields = extract_apply_fields(frame, frame_id="icims")
            self._record_step(steps, f"Extracted {len(fields)} visible iCIMS application fields.")

            if self._is_email_gate(frame):
                raise RuntimeError(
                    "iCIMS did not advance past the email step. This flow may require hCaptcha or manual interaction."
                )
            if not self._has_application_form(fields):
                raise RuntimeError("iCIMS application form was not detected after the email step.")

            self._fill_core_fields(frame, fields, applicant)
            self._record_step(steps, "Filled iCIMS core applicant fields.")

            self._upload_resume(frame, fields, resume_path)
            self._record_step(steps, "Uploaded resume PDF.")

            self._fill_known_questions(frame, fields, applicant)
            self._record_step(steps, "Filled iCIMS known application questions.")

            self._answer_custom_fields(frame, fields, applicant, job, answer_service, cancel_checker)
            self._record_step(steps, "Filled iCIMS custom answers.")

            if not auto_submit:
                page.screenshot(path=str(screenshot_path), full_page=True)
                self._record_step(steps, "Captured review screenshot.")
                return ApplyJobResult(
                    job_id=job.id,
                    company_name=job.company_name,
                    job_title=job.job_title,
                    ats_type=self.ats_type,
                    status="needs_review",
                    success=False,
                    screenshot_path=str(screenshot_path),
                    confirmation_text="Application filled but auto-submit is disabled.",
                    steps=steps,
                )

            self._submit(frame)
            self._record_step(steps, "Submitted iCIMS application.")
            confirmation_text = self._verify_success(frame)
            page.screenshot(path=str(screenshot_path), full_page=True)
            self._record_step(steps, f"Captured success screenshot at {screenshot_path}.")
            logger.info(
                "iCIMS apply verified. job_id=%s confirmation=%s screenshot=%s",
                job.id,
                confirmation_text,
                screenshot_path,
            )
            return ApplyJobResult(
                job_id=job.id,
                company_name=job.company_name,
                job_title=job.job_title,
                ats_type=self.ats_type,
                status="submitted",
                success=True,
                screenshot_path=str(screenshot_path),
                confirmation_text=confirmation_text,
                steps=steps,
            )
        except Exception as exc:
            if str(exc) == "Apply agent cancelled.":
                raise
            captured_path: str | None = None
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                captured_path = str(screenshot_path)
                self._record_step(steps, f"Captured failure screenshot at {screenshot_path}.")
            except Exception:
                pass
            self._record_step(steps, f"Failed: {exc}")
            logger.exception(
                "iCIMS apply failed. job_id=%s company=%s title=%s screenshot=%s steps=%s",
                job.id,
                job.company_name,
                job.job_title,
                captured_path,
                " | ".join(steps),
            )
            return ApplyJobResult(
                job_id=job.id,
                company_name=job.company_name,
                job_title=job.job_title,
                ats_type=self.ats_type,
                status="failed",
                success=False,
                error=str(exc),
                screenshot_path=captured_path,
                steps=steps,
            )

    def _resolve_icims_frame(self, page: Page):
        page.wait_for_timeout(2500)
        for frame in page.frames:
            if _is_icims_frame_url(frame.url):
                return frame
        if _is_icims_frame_url(page.url):
            return page.main_frame
        raise RuntimeError("iCIMS application frame was not found.")

    def _advance_email_gate(self, page: Page, frame, applicant: ApplicantProfileConfig, cancel_checker, steps: list[str]):
        if not self._is_email_gate(frame):
            return frame

        email_input = frame.locator("#email")
        if email_input.count() == 0:
            raise RuntimeError("iCIMS email field was not found.")
        email_input.first.fill(applicant.email, timeout=10000)
        submit = frame.locator("#enterEmailSubmitButton")
        if submit.count() == 0:
            raise RuntimeError("iCIMS email step submit button was not found.")
        submit.first.click(timeout=10000)
        self._record_step(steps, "Submitted iCIMS email gate.")

        deadline_ms = 90000
        elapsed = 0
        while elapsed <= deadline_ms:
            self._check_cancelled(cancel_checker)
            page.wait_for_timeout(1000)
            frame = self._resolve_icims_frame(page)
            if not self._is_email_gate(frame):
                return frame
            elapsed += 1000

        return frame

    def _is_email_gate(self, frame) -> bool:
        try:
            email_visible = frame.locator("#email").count() > 0 and frame.locator("#email").first.is_visible()
            submit_visible = frame.locator("#enterEmailSubmitButton").count() > 0
            header_text = normalized_text(" ".join(frame.locator("h1").all_inner_texts()))
            return email_visible and submit_visible and "enter your information" in header_text
        except PlaywrightError:
            return False

    def _has_password_gate(self, frame) -> bool:
        try:
            password = frame.locator("input[type='password']")
            return password.count() > 0 and password.first.is_visible()
        except PlaywrightError:
            return False

    def _has_application_form(self, fields: list[ApplyField]) -> bool:
        if len(fields) >= 3:
            return True
        return any(
            field.tag == "input" and field.type == "file"
            for field in fields
        )

    def _fill_core_fields(self, frame, fields: list[ApplyField], applicant: ApplicantProfileConfig) -> None:
        first_name, last_name = _split_name(applicant)
        self._fill_matching_field(frame, fields, ("first name",), first_name)
        self._fill_matching_field(frame, fields, ("last name",), last_name)
        self._fill_matching_field(frame, fields, ("full name", "legal name"), applicant.legal_name)
        self._fill_matching_field(frame, fields, ("preferred name",), applicant.preferred_name or first_name)
        self._fill_matching_field(frame, fields, ("email", "email address"), applicant.email)
        self._fill_matching_field(frame, fields, ("phone", "phone number", "mobile"), applicant.phone)
        self._fill_matching_field(frame, fields, ("address", "street address"), applicant.address_line_1)
        self._fill_matching_field(frame, fields, ("address line 2", "apartment"), applicant.address_line_2)
        self._fill_matching_field(frame, fields, ("city",), applicant.city)
        self._fill_matching_field(frame, fields, ("state", "province", "region"), applicant.state)
        self._fill_matching_field(frame, fields, ("zip", "postal"), applicant.postal_code)
        self._fill_matching_field(frame, fields, ("country",), applicant.country)
        self._fill_matching_field(frame, fields, ("linkedin",), applicant.linkedin_url)
        self._fill_matching_field(frame, fields, ("portfolio", "website"), applicant.portfolio_url or applicant.github_url)
        self._fill_matching_field(frame, fields, ("current company", "employer"), applicant.current_company)
        self._fill_matching_field(frame, fields, ("current title", "job title"), applicant.current_title)
        self._fill_matching_field(frame, fields, ("years of experience",), applicant.years_of_experience)

    def _upload_resume(self, frame, fields: list[ApplyField], resume_path: Path) -> None:
        preferred = [
            field for field in fields
            if field.tag == "input" and field.type == "file" and "resume" in normalized_text(field.label)
        ]
        candidates = preferred or [field for field in fields if field.tag == "input" and field.type == "file"]
        for field in candidates:
            locator = frame.locator(field.selector)
            if locator.count() == 0:
                continue
            locator.first.set_input_files(str(resume_path), timeout=10000)
            return
        raise RuntimeError("iCIMS resume upload field was not found.")

    def _fill_known_questions(self, frame, fields: list[ApplyField], applicant: ApplicantProfileConfig) -> None:
        self._fill_matching_field(frame, fields, ("compensation", "salary"), applicant.compensation_expectation)
        self._fill_matching_field(frame, fields, ("programming languages",), applicant.programming_languages_years)
        self._fill_matching_field(frame, fields, ("favorite ai tool",), applicant.favorite_ai_tool)
        self._fill_matching_field(frame, fields, ("how you use your favorite ai tool",), applicant.favorite_ai_tool_usage)
        self._fill_yes_no_field(frame, fields, ("authorized to work", "legally authorized"), applicant.work_authorized_us)
        self._fill_yes_no_field(frame, fields, ("require sponsorship", "need sponsorship"), applicant.requires_sponsorship)
        self._fill_yes_no_field(frame, fields, ("sms", "text message"), applicant.sms_consent)

    def _answer_custom_fields(
        self,
        frame,
        fields: list[ApplyField],
        applicant: ApplicantProfileConfig,
        job: Job,
        answer_service: ApplicationAnswerService,
        cancel_checker,
    ) -> None:
        for field in fields:
            self._check_cancelled(cancel_checker)
            if not self._is_custom_answer_field(field):
                continue
            locator = frame.locator(field.selector)
            if locator.count() == 0:
                continue
            try:
                current_value = locator.input_value(timeout=1000).strip()
            except PlaywrightError:
                current_value = ""
            if current_value:
                continue
            answer = answer_service.generate_custom_answer(
                question=field.label,
                applicant=applicant,
                job=job,
            )
            if answer:
                locator.first.fill(answer[:3000], timeout=10000)

    def _fill_matching_field(self, frame, fields: list[ApplyField], keywords: tuple[str, ...], value: str) -> None:
        if not value:
            return
        for field in fields:
            label = normalized_text(field.label)
            if not any(keyword in label for keyword in keywords):
                continue
            if field.tag not in {"input", "textarea"}:
                continue
            if field.type in {"file", "checkbox", "radio", "submit", "button", "password", "hidden"}:
                continue
            locator = frame.locator(field.selector)
            if locator.count() == 0:
                continue
            try:
                current = locator.first.input_value(timeout=1000).strip()
            except PlaywrightError:
                current = ""
            if current:
                return
            locator.first.fill(value, timeout=10000)
            return

    def _fill_yes_no_field(self, frame, fields: list[ApplyField], keywords: tuple[str, ...], answer: bool) -> None:
        expected = "yes" if answer else "no"
        for field in fields:
            label = normalized_text(field.label)
            if not any(keyword in label for keyword in keywords):
                continue
            locator = frame.locator(field.selector)
            if locator.count() == 0:
                continue
            if field.tag == "select":
                try:
                    locator.first.select_option(label=expected.capitalize(), timeout=10000)
                    return
                except PlaywrightError:
                    try:
                        locator.first.select_option(expected, timeout=10000)
                        return
                    except PlaywrightError:
                        continue
            if field.type in {"radio", "checkbox"}:
                try:
                    frame.get_by_label(re.compile(fr"{expected}", re.IGNORECASE)).first.check(timeout=10000)
                    return
                except PlaywrightError:
                    continue

    def _submit(self, frame) -> None:
        candidates = (
            frame.get_by_role("button", name=re.compile(r"submit|apply|continue", re.IGNORECASE)),
            frame.locator("input[type='submit'][value]"),
        )
        for locator in candidates:
            if locator.count() == 0:
                continue
            try:
                locator.first.click(timeout=10000)
                frame.wait_for_timeout(6000)
                return
            except PlaywrightError:
                continue
        raise RuntimeError("iCIMS submit button was not found.")

    def _verify_success(self, frame) -> str:
        try:
            body_text = frame.locator("body").inner_text(timeout=8000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Unable to read post-submit iCIMS confirmation state.") from exc

        normalized = normalized_text(body_text)
        for term in SUCCESS_TERMS:
            if term in normalized:
                return _first_confirmation_line(body_text)
        excerpt = _body_excerpt(body_text)
        raise RuntimeError(f"Could not verify successful iCIMS application submission. {excerpt}")

    def _is_custom_answer_field(self, field: ApplyField) -> bool:
        label = normalized_text(field.label)
        if not label:
            return False
        if any(
            keyword in label
            for keyword in (
                "first name",
                "last name",
                "full name",
                "preferred name",
                "email",
                "phone",
                "address",
                "city",
                "state",
                "postal",
                "zip",
                "country",
                "linkedin",
                "portfolio",
                "website",
                "current company",
                "current title",
                "years of experience",
                "compensation",
                "salary",
                "authorized to work",
                "sponsorship",
                "sms",
                "resume",
                "upload",
            )
        ):
            return False
        if field.tag not in {"input", "textarea"}:
            return False
        if field.type in {"file", "checkbox", "radio", "submit", "button", "password", "hidden"}:
            return False
        return True

    def _check_cancelled(self, cancel_checker) -> None:
        if cancel_checker is not None and cancel_checker():
            raise RuntimeError("Apply agent cancelled.")

    @staticmethod
    def _record_step(steps: list[str], message: str) -> None:
        logger.info("iCIMS apply step: %s", message)
        steps.append(message)


def _is_icims_frame_url(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return "icims.com/jobs/" in lowered and "in_iframe=1" in lowered


def _split_name(applicant: ApplicantProfileConfig) -> tuple[str, str]:
    name = (applicant.legal_name or "").strip()
    if not name:
        return "", ""
    parts = [part for part in name.split() if part]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _first_confirmation_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:240]
    return "Application submitted."


def _body_excerpt(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if not compact:
        return "No confirmation text was found."
    return compact[:limit]
