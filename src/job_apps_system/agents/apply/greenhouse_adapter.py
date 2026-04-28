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
    "your application has been submitted",
    "we have received your application",
    "application submitted",
    "application has been submitted",
    "application was successfully submitted",
)


class GreenhouseApplyAdapter:
    ats_type = "greenhouse"

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
            "Greenhouse apply started. job_id=%s company=%s title=%s page_url=%s",
            job.id,
            job.company_name,
            job.job_title,
            page.url,
        )
        try:
            self._check_cancelled(cancel_checker)

            frame = self._resolve_greenhouse_frame(page)
            self._record_step(steps, "Resolved Greenhouse application frame.")
            self._clear_embedded_privacy_overlay(frame)
            self._record_step(steps, "Cleared Greenhouse privacy overlay when present.")
            self._ensure_apply_form_visible(frame)
            self._record_step(steps, "Confirmed Greenhouse application form is visible.")
            self._check_cancelled(cancel_checker)

            fields = extract_apply_fields(frame, frame_id="greenhouse")
            self._record_step(steps, f"Extracted {len(fields)} visible Greenhouse application fields.")

            self._fill_core_fields(frame, applicant)
            self._record_step(steps, "Filled Greenhouse core applicant fields.")

            self._upload_resume(frame, resume_path)
            self._record_step(steps, "Uploaded resume PDF.")

            self._fill_known_questions(frame, fields, applicant)
            self._record_step(steps, "Filled Greenhouse known application questions.")

            self._answer_custom_fields(frame, fields, applicant, job, answer_service, cancel_checker)
            self._record_step(steps, "Filled Greenhouse custom answers.")

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
            self._record_step(steps, "Submitted Greenhouse application.")
            confirmation_text = self._verify_success(frame)
            page.screenshot(path=str(screenshot_path), full_page=True)
            self._record_step(steps, f"Captured success screenshot at {screenshot_path}.")
            logger.info(
                "Greenhouse apply verified. job_id=%s confirmation=%s screenshot=%s",
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
                "Greenhouse apply failed. job_id=%s company=%s title=%s screenshot=%s steps=%s",
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

    def _resolve_greenhouse_frame(self, page: Page):
        page.wait_for_timeout(2500)
        if _is_greenhouse_frame_url(page.url):
            return page.main_frame

        deadline_ms = 15000
        elapsed = 0
        while elapsed <= deadline_ms:
            for frame in page.frames:
                if _is_greenhouse_frame_url(frame.url):
                    return frame
            page.wait_for_timeout(500)
            elapsed += 500

        raise RuntimeError("Greenhouse application frame was not found.")

    def _ensure_apply_form_visible(self, frame) -> None:
        if self._has_visible_form(frame):
            return
        button = frame.get_by_role("button", name=re.compile(r"^apply$", re.IGNORECASE))
        if button.count() == 0:
            button = frame.get_by_role("link", name=re.compile(r"^apply$", re.IGNORECASE))
        if button.count() == 0:
            raise RuntimeError("Greenhouse Apply button was found but the application form is not visible.")
        button.first.click(timeout=10000)
        frame.wait_for_timeout(2000)
        if not self._has_visible_form(frame):
            raise RuntimeError("Greenhouse application form did not open after clicking Apply.")

    def _has_visible_form(self, frame) -> bool:
        selectors = (
            "#first_name",
            "#email",
            "#resume",
            "button[type='submit']",
        )
        for selector in selectors:
            locator = frame.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                return True
        return False

    def _clear_embedded_privacy_overlay(self, frame) -> None:
        try:
            frame.evaluate(
                """
                () => {
                  const selectors = [
                    '#lanyard_root',
                    '[data-ketch-backdrop="true"]',
                    '[id^="ketch-consent"]',
                    '[id^="ketch-banner"]',
                  ];
                  for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                      node.remove();
                    }
                  }
                }
                """
            )
        except PlaywrightError:
            pass
        frame.wait_for_timeout(250)

    def _fill_core_fields(self, frame, applicant: ApplicantProfileConfig) -> None:
        first_name, last_name = _split_name(applicant)
        self._fill_if_present(frame, "#first_name", first_name)
        self._fill_if_present(frame, "#last_name", last_name)
        self._fill_if_present(frame, "#email", applicant.email)
        self._fill_if_present(frame, "#phone", applicant.phone)
        self._select_combobox_value(frame, "#country", applicant.country, exact=False)
        self._select_combobox_value(frame, "#candidate-location", applicant.location_summary or applicant.full_address, exact=True)

    def _upload_resume(self, frame, resume_path: Path) -> None:
        selectors = (
            "#resume",
            "input[type='file']#resume",
            "input[type='file']",
        )
        for selector in selectors:
            locator = frame.locator(selector)
            if locator.count() == 0:
                continue
            candidate = locator.first if selector != "input[type='file']" else locator.filter(has_not=frame.locator("#cover_letter")).first
            candidate.set_input_files(str(resume_path), timeout=10000)
            return
        raise RuntimeError("Greenhouse resume upload field was not found.")

    def _fill_known_questions(self, frame, fields: list[ApplyField], applicant: ApplicantProfileConfig) -> None:
        self._fill_matching_field(frame, fields, "linkedin profile", applicant.linkedin_url)
        self._fill_matching_field(frame, fields, "website", applicant.portfolio_url or applicant.github_url)
        self._select_matching_combobox(frame, fields, "require sponsorship", "Yes" if applicant.requires_sponsorship else "No")

        athlete_field = next(
            (
                field
                for field in fields
                if "athlete" in normalized_text(field.label) or "esports competitor" in normalized_text(field.label)
            ),
            None,
        )
        if athlete_field is not None:
            athlete_or_esports_competitor = getattr(applicant, "athlete_or_esports_competitor", None)
            self._select_combobox_value(
                frame,
                athlete_field.selector,
                "Yes" if athlete_or_esports_competitor is True else "No",
                exact=True,
            )

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
                logger.info(
                    "Greenhouse custom answer field. selector=%s label=%s",
                    field.selector,
                    field.label,
                )
                locator.fill(answer[:3000], timeout=10000)

    def _submit(self, frame) -> None:
        button = frame.locator("button[type='submit']")
        if button.count() == 0:
            button = frame.get_by_role("button", name=re.compile(r"submit application", re.IGNORECASE))
        if button.count() == 0:
            raise RuntimeError("Greenhouse submit button was not found.")
        button.first.click(timeout=10000)
        frame.wait_for_timeout(5000)

    def _verify_success(self, frame) -> str:
        try:
            body_text = frame.locator("body").inner_text(timeout=8000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Unable to read Greenhouse post-submit confirmation state.") from exc

        normalized = normalized_text(body_text)
        if self._has_success_text(body_text):
            return _first_confirmation_line(body_text)

        excerpt = _body_excerpt(body_text)
        if "captcha" in normalized or "recaptcha" in normalized:
            raise RuntimeError(f"Application submit appears blocked by CAPTCHA. Visible text: {excerpt}")
        if "required" in normalized or "please complete" in normalized or "this field is required" in normalized:
            raise RuntimeError(f"Greenhouse submit did not complete; required-field validation is still visible. Visible text: {excerpt}")
        raise RuntimeError(f"Could not verify successful Greenhouse application submission. Visible text: {excerpt}")

    def _has_success_text(self, body_text: str) -> bool:
        normalized = normalized_text(body_text)
        return any(term in normalized for term in SUCCESS_TERMS)

    def _fill_matching_field(self, frame, fields: list[ApplyField], keyword: str, value: str | None) -> bool:
        if not value:
            return False
        field = next((item for item in fields if keyword in normalized_text(item.label)), None)
        if not field:
            return False
        frame.locator(field.selector).first.fill(value, timeout=8000)
        return True

    def _select_matching_combobox(self, frame, fields: list[ApplyField], keyword: str, value: str | None) -> bool:
        if not value:
            return False
        field = next((item for item in fields if keyword in normalized_text(item.label)), None)
        if not field:
            return False
        return self._select_combobox_value(frame, field.selector, value, exact=True)

    def _fill_field_selector(self, frame, selector: str, value: str) -> None:
        locator = frame.locator(selector)
        if locator.count() == 0:
            return
        target = locator.first
        target.scroll_into_view_if_needed(timeout=5000)
        target.fill("")
        target.type(value, delay=10)

    def _fill_if_present(self, frame, selector: str, value: str | None) -> bool:
        if not value:
            return False
        locator = frame.locator(selector)
        if locator.count() == 0:
            return False
        locator.first.fill(value, timeout=8000)
        return True

    def _select_combobox_value(self, frame, selector: str, value: str | None, *, exact: bool) -> bool:
        if not value:
            return False
        locator = frame.locator(selector)
        if locator.count() == 0:
            return False
        field = locator.first
        field.click(timeout=8000, force=True)
        try:
            field.fill("")
        except PlaywrightError:
            pass
        field.fill(value, timeout=8000)
        frame.wait_for_timeout(1200)
        option = _match_option(frame, value, exact=exact)
        if option is None:
            return False
        option.click(timeout=5000, force=True)
        frame.wait_for_timeout(300)
        return True

    def _is_custom_answer_field(self, field: ApplyField) -> bool:
        field_type = (field.type or "").lower()
        if field.tag not in {"input", "textarea"}:
            return False
        if field_type in {"hidden", "file", "radio", "checkbox", "email", "tel", "url", "search"}:
            return False
        label = normalized_text(field.label)
        if label in {"", "select...", "select", "attach", "attachattach"}:
            return False
        skip_tokens = (
            "first name",
            "last name",
            "email",
            "phone",
            "country",
            "location",
            "linkedin",
            "website",
            "authorized",
            "sponsor",
            "athlete",
            "esports",
            "gender",
            "hispanic",
            "veteran",
            "disability",
            "resume",
            "cover letter",
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

    def _record_step(self, steps: list[str], message: str) -> None:
        steps.append(message)
        logger.info("Greenhouse apply step: %s", message)

    def _check_cancelled(self, cancel_checker) -> None:
        if cancel_checker and cancel_checker():
            raise RuntimeError("Apply agent cancelled.")


def _split_name(applicant: ApplicantProfileConfig) -> tuple[str, str]:
    name = (applicant.legal_name or "").strip()
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        preferred = (applicant.preferred_name or "").strip()
        return preferred or parts[0], parts[0]
    first_name = (applicant.preferred_name or "").strip() or parts[0]
    last_name = " ".join(parts[1:])
    return first_name, last_name


def _match_option(frame, value: str, *, exact: bool):
    pattern = re.compile(rf"^{re.escape(value)}$", re.IGNORECASE) if exact else re.compile(rf"^{re.escape(value)}(?:\\b|\\s|$)", re.IGNORECASE)
    option = frame.get_by_role("option", name=pattern)
    if option.count() > 0:
        return option.first
    options = frame.get_by_role("option")
    if options.count() == 0:
        return None
    for text in options.all_inner_texts():
        normalized = normalized_text(text)
        target = normalized_text(value)
        if exact and normalized == target:
            return frame.get_by_role("option", name=re.compile(re.escape(text), re.IGNORECASE)).first
        if not exact and normalized.startswith(target):
            return frame.get_by_role("option", name=re.compile(re.escape(text), re.IGNORECASE)).first
    return None


def _is_greenhouse_frame_url(url: str | None) -> bool:
    normalized = (url or "").lower()
    return "job-boards.greenhouse.io" in normalized or "boards.greenhouse.io" in normalized


def _first_confirmation_line(body_text: str) -> str:
    for line in body_text.splitlines():
        cleaned = line.strip()
        if cleaned and len(cleaned) > 6:
            return cleaned
    return "Application submitted."


def _body_excerpt(body_text: str, max_length: int = 320) -> str:
    compact = " ".join(body_text.split())
    return compact[:max_length] if compact else "No visible text captured."
