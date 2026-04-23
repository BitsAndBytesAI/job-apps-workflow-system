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
    "application submitted",
    "application has been submitted",
    "application was successfully submitted",
    "your application was successfully submitted",
    "thank you for applying",
    "thank you for your application",
    "received your application",
    "we have received your application",
)


class AshbyApplyAdapter:
    ats_type = "ashby"

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
        logger.info("Ashby apply started. job_id=%s company=%s title=%s page_url=%s", job.id, job.company_name, job.job_title, page.url)
        try:
            self._check_cancelled(cancel_checker)

            frame = self._resolve_ashby_frame(page)
            self._record_step(steps, "Resolved Ashby frame.")
            self._click_application_tab(frame)
            self._record_step(steps, "Opened Application tab.")
            self._check_cancelled(cancel_checker)

            fields = extract_apply_fields(frame, frame_id="ashby")
            self._record_step(steps, f"Extracted {len(fields)} visible application fields.")
            self._upload_resume(frame, resume_path)
            self._record_step(steps, "Uploaded resume PDF.")
            self._fill_known_fields(frame, fields, applicant)
            self._record_step(steps, "Filled applicant profile fields.")
            self._answer_custom_fields(frame, fields, applicant, job, answer_service, cancel_checker)
            self._record_step(steps, "Filled custom application answers.")

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

            submit_attempts = self._submit(frame)
            self._record_step(steps, f"Submitted application after {submit_attempts} click(s).")
            confirmation_text = self._verify_success(frame)
            page.screenshot(path=str(screenshot_path), full_page=True)
            self._record_step(steps, f"Captured success screenshot at {screenshot_path}.")

            logger.info(
                "Ashby apply verified. job_id=%s confirmation=%s screenshot=%s",
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
                "Ashby apply failed. job_id=%s company=%s title=%s screenshot=%s steps=%s",
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

    def _resolve_ashby_frame(self, page: Page):
        page.wait_for_timeout(2500)
        if "jobs.ashbyhq.com" in page.url:
            return page.main_frame

        deadline_ms = 15000
        elapsed = 0
        while elapsed <= deadline_ms:
            for frame in page.frames:
                if "jobs.ashbyhq.com" in frame.url:
                    return frame
            page.wait_for_timeout(500)
            elapsed += 500

        raise RuntimeError("Ashby application frame was not found.")

    def _click_application_tab(self, frame) -> None:
        if "/application" in frame.url:
            return
        tab = frame.get_by_role("tab", name=re.compile(r"application", re.IGNORECASE))
        if tab.count() == 0:
            tab = frame.get_by_role("link", name=re.compile(r"application", re.IGNORECASE))
        if tab.count() == 0:
            tab = frame.locator("a[href*='/application']")
        if tab.count() == 0:
            tab = frame.get_by_text("Application", exact=True)
        tab.first.click(timeout=10000)
        frame.wait_for_timeout(2500)

    def _upload_resume(self, frame, resume_path: Path) -> None:
        selectors = [
            "input#_systemfield_resume[type='file']",
            "input[id='_systemfield_resume']",
            "input[type='file']",
        ]
        for selector in selectors:
            locator = frame.locator(selector)
            if locator.count() == 0:
                continue
            candidate = locator.last if selector == "input[type='file']" else locator.first
            candidate.set_input_files(str(resume_path), timeout=10000)
            return
        raise RuntimeError("Resume upload field was not found.")

    def _fill_known_fields(self, frame, fields: list[ApplyField], applicant: ApplicantProfileConfig) -> None:
        self._fill_if_present(frame, "input[name='_systemfield_name']", applicant.legal_name)
        self._fill_if_present(frame, "input[name='_systemfield_email']", applicant.email)
        self._fill_if_present(frame, "input[type='tel']", applicant.phone)
        self._fill_if_present(frame, "input[type='url']", applicant.linkedin_url)
        self._fill_matching_field(frame, fields, "current address", applicant.full_address)
        self._fill_location(frame, applicant)
        self._fill_matching_field(frame, fields, "annual compensation", _digits_only(applicant.compensation_expectation))
        self._fill_matching_field(frame, fields, "compensation expectations", _digits_only(applicant.compensation_expectation))
        self._set_sms_consent(frame, applicant.sms_consent)
        self._set_work_authorization(frame, fields, applicant.work_authorized_us and not applicant.requires_sponsorship)

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
            try:
                current_value = locator.input_value(timeout=1000).strip()
            except PlaywrightError:
                current_value = ""
            if current_value:
                continue
            answer = self._known_custom_answer(field, applicant)
            if not answer:
                answer = answer_service.generate_custom_answer(
                    question=field.label,
                    applicant=applicant,
                    job=job,
                    constraints=self._answer_constraints(field),
                )
            if answer:
                locator.fill(answer[:3000], timeout=10000)

    def _submit(self, frame) -> int:
        button = frame.get_by_role("button", name=re.compile(r"submit application", re.IGNORECASE))
        if button.count() == 0:
            raise RuntimeError("Submit Application button was not found.")

        attempts = 0
        button.first.click(timeout=10000)
        attempts += 1
        logger.info("Ashby submit click attempt %s", attempts)
        frame.wait_for_timeout(2500)
        if self._is_success_state(frame):
            return attempts

        button = frame.get_by_role("button", name=re.compile(r"submit application", re.IGNORECASE))
        if button.count() > 0:
            try:
                if button.first.is_enabled():
                    button.first.click(timeout=10000)
                    attempts += 1
                    logger.info("Ashby submit click attempt %s", attempts)
                    frame.wait_for_timeout(6000)
            except PlaywrightError:
                pass

        return attempts

    def _verify_success(self, frame) -> str:
        try:
            body_text = frame.locator("body").inner_text(timeout=8000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Unable to read post-submit confirmation state.") from exc

        normalized = normalized_text(body_text)
        if self._has_success_text(body_text):
            return _first_confirmation_line(body_text)

        excerpt = _body_excerpt(body_text)
        if "captcha" in normalized or "recaptcha" in normalized:
            raise RuntimeError(f"Application submit appears blocked by CAPTCHA. Visible text: {excerpt}")
        if "required" in normalized or "please complete" in normalized or "this field" in normalized:
            raise RuntimeError(f"Application submit did not complete; required-field validation is still visible. Visible text: {excerpt}")
        raise RuntimeError(f"Could not verify successful application submission. Visible text: {excerpt}")

    def _is_success_state(self, frame) -> bool:
        try:
            body_text = frame.locator("body").inner_text(timeout=2000)
        except PlaywrightError:
            return False
        return self._has_success_text(body_text)

    def _has_success_text(self, body_text: str) -> bool:
        normalized = normalized_text(body_text)
        return any(term in normalized for term in SUCCESS_TERMS)

    def _fill_if_present(self, frame, selector: str, value: str) -> bool:
        if not value:
            return False
        locator = frame.locator(selector)
        if locator.count() == 0:
            return False
        locator.first.fill(value, timeout=8000)
        return True

    def _fill_matching_field(self, frame, fields: list[ApplyField], label_token: str, value: str) -> bool:
        if not value:
            return False
        token = normalized_text(label_token)
        for field in fields:
            if token in normalized_text(field.label):
                frame.locator(field.selector).first.fill(value, timeout=8000)
                return True
        return False

    def _fill_location(self, frame, applicant: ApplicantProfileConfig) -> None:
        value = applicant.location_summary or applicant.full_address
        if not value:
            return
        locator = frame.locator("input[placeholder='Start typing...']")
        if locator.count() == 0:
            return
        field = locator.first
        field.fill(value, timeout=8000)
        frame.wait_for_timeout(1200)
        try:
            field.press("ArrowDown")
            field.press("Enter")
        except PlaywrightError:
            pass

    def _set_sms_consent(self, frame, consent: bool) -> None:
        desired_value = "given" if consent else "notGiven"
        desired_input = frame.locator(f"input[name='communicationConsent'][value='{desired_value}']")
        if desired_input.count() == 0:
            return

        if desired_input.first.is_checked():
            return

        label = frame.locator(f"label:has(input[name='communicationConsent'][value='{desired_value}'])")
        if label.count() > 0:
            try:
                label.first.click(timeout=5000)
            except PlaywrightError:
                pass

        if desired_input.first.is_checked():
            return

        try:
            desired_input.first.evaluate(
                """
                (el) => {
                  el.click();
                  if (!el.checked) {
                    el.checked = true;
                    el.dispatchEvent(new Event("input", { bubbles: true }));
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                  }
                  return el.checked;
                }
                """
            )
        except PlaywrightError as exc:
            raise RuntimeError(f"Unable to set SMS consent to {desired_value}.") from exc

        if not desired_input.first.is_checked():
            raise RuntimeError(f"Unable to set SMS consent to {desired_value}.")

    def _set_work_authorization(self, frame, fields: list[ApplyField], authorized: bool) -> None:
        for field in fields:
            label_text = normalized_text(field.label)
            is_work_auth_field = (
                ("work in the united states" in label_text or "legally authorized" in label_text or "legally authroized" in label_text)
                and ("sponsor" in label_text or "sponsorhip" in label_text or "sponsorship" in label_text)
            )
            if not is_work_auth_field:
                continue
            locator = frame.locator(field.selector).first
            if field.type == "checkbox":
                if authorized:
                    locator.check(force=True, timeout=5000)
                else:
                    locator.uncheck(force=True, timeout=5000)
                return
            if field.type == "radio":
                locator.check(force=True, timeout=5000)
                return

        label = "Yes" if authorized else "No"
        buttons = frame.get_by_role("button", name=re.compile(f"^{label}$", re.IGNORECASE))
        if buttons.count() > 0:
            buttons.first.click(timeout=5000)

    def _is_custom_answer_field(self, field: ApplyField) -> bool:
        field_type = (field.type or "").lower()
        if field.tag not in {"input", "textarea"}:
            return False
        if field_type in {"hidden", "file", "radio", "checkbox", "email", "tel", "url"}:
            return False
        label = normalized_text(field.label)
        skip_tokens = (
            "legal name",
            "email",
            "phone",
            "location",
            "current address",
            "linkedin",
            "authorized",
            "sponsor",
            "compensation",
            "annual compensation",
            "resume",
            "autofill",
        )
        return not any(token in label for token in skip_tokens)

    def _known_custom_answer(self, field: ApplyField, applicant: ApplicantProfileConfig) -> str:
        label = normalized_text(field.label)
        if "programming languages" in label:
            return applicant.programming_languages_years.strip()
        if "favorite ai tool" in label:
            parts = [applicant.favorite_ai_tool.strip(), applicant.favorite_ai_tool_usage.strip()]
            return " — ".join(part for part in parts if part)
        if "company value" in label or "company values" in label or "exemplified" in label:
            return applicant.company_value_example.strip()
        if "why are you interested" in label or "why interested" in label or "why do you want" in label:
            return applicant.why_interested_guidance.strip()
        if "anything else" in label or "additional information" in label or "additional info" in label:
            return applicant.additional_info_guidance.strip()
        return ""

    def _answer_constraints(self, field: ApplyField) -> str:
        if field.tag == "textarea":
            return "Answer in 2-5 concise sentences unless the question asks for a list."
        return "Answer in one concise sentence or phrase."

    def _check_cancelled(self, cancel_checker) -> None:
        if cancel_checker is not None and cancel_checker():
            raise RuntimeError("Apply agent cancelled.")

    def _record_step(self, steps: list[str], message: str) -> None:
        steps.append(message)
        logger.info("Ashby apply step: %s", message)


def _digits_only(value: str) -> str:
    digits = re.sub(r"[^\d]", "", value or "")
    return digits


def _first_confirmation_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if any(term in normalized_text(line) for term in SUCCESS_TERMS):
            return line
    return lines[0] if lines else "Application submitted."


def _body_excerpt(text: str, limit: int = 240) -> str:
    flattened = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(flattened) <= limit:
        return flattened
    return f"{flattened[:limit].rstrip()}…"
