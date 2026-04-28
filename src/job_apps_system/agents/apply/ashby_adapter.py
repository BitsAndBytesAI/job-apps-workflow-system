from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
            self._ensure_job_detail_page(frame, job)
            self._record_step(steps, "Opened Ashby job detail page.")
            self._click_application_tab(frame)
            self._record_step(steps, "Opened Application tab.")
            self._check_cancelled(cancel_checker)

            fields = extract_apply_fields(frame, frame_id="ashby")
            self._record_step(steps, f"Extracted {len(fields)} visible application fields.")
            self._upload_resume(frame, resume_path)
            self._record_step(steps, "Uploaded resume PDF.")
            self._fill_known_fields(frame, fields, applicant)
            self._record_step(steps, "Filled applicant profile fields.")
            self._answer_binary_questions(frame, applicant)
            self._record_step(steps, "Filled binary application questions.")
            self._answer_choice_fields(frame, fields, applicant, answer_service)
            self._record_step(steps, "Filled choice application questions.")
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

    def _ensure_job_detail_page(self, frame, job: Job) -> None:
        if self._is_job_detail_page(frame):
            return

        ashby_job_id = _extract_ashby_job_id(job.apply_url or frame.url)
        detail_link = self._detail_link_locator(frame, ashby_job_id=ashby_job_id, job_title=job.job_title)
        if detail_link.count() == 0:
            return

        starting_url = frame.url
        detail_link.first.click(timeout=10000)
        self._wait_for_job_detail_page(frame, starting_url=starting_url)

    def _detail_link_locator(self, frame, *, ashby_job_id: str, job_title: str):
        if ashby_job_id:
            locator = frame.locator(f"a[href*='/{ashby_job_id}']")
            if locator.count() > 0:
                return locator
        if job_title:
            locator = frame.get_by_role("link", name=re.compile(re.escape(job_title), re.IGNORECASE))
            if locator.count() > 0:
                return locator
        return frame.locator("__apply_agent_noop__")

    def _wait_for_job_detail_page(self, frame, *, starting_url: str) -> None:
        elapsed = 0
        while elapsed <= 15000:
            if frame.url != starting_url and self._is_job_detail_page(frame):
                return
            if self._is_job_detail_page(frame):
                return
            frame.wait_for_timeout(500)
            elapsed += 500
        raise RuntimeError("Ashby job detail page did not open from the listing page.")

    def _is_job_detail_page(self, frame) -> bool:
        if "/application" in frame.url:
            return True
        if re.search(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", frame.url, re.IGNORECASE):
            return True
        application_controls = [
            frame.get_by_role("tab", name=re.compile(r"application", re.IGNORECASE)),
            frame.get_by_role("link", name=re.compile(r"application", re.IGNORECASE)),
            frame.locator("a[href*='/application']"),
            frame.get_by_role("link", name=re.compile(r"apply for this job", re.IGNORECASE)),
        ]
        return any(locator.count() > 0 for locator in application_controls)

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

    def _answer_choice_fields(
        self,
        frame,
        fields: list[ApplyField],
        applicant: ApplicantProfileConfig,
        answer_service: ApplicationAnswerService,
    ) -> None:
        self._select_team_size_radio(frame, fields, applicant, answer_service.resume_text)
        self._select_technology_checkboxes(frame, fields, answer_service.resume_text)

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
            used_llm = False
            if not answer:
                used_llm = True
                answer = answer_service.generate_custom_answer(
                    question=field.label,
                    applicant=applicant,
                    job=job,
                    constraints=self._answer_constraints(field),
                    ats_type=self.ats_type,
                    field_type=field.type or field.tag,
                    required=field.required,
                )
            if answer:
                self._fill_custom_answer(locator, field, answer)
            elif not used_llm:
                answer_service.record_unanswered_field(
                    job=job,
                    field=field,
                    ats_type=self.ats_type,
                    reason="blank_after_inference",
                )

    def _answer_binary_questions(self, frame, applicant: ApplicantProfileConfig) -> None:
        for question_tokens, answer in self._binary_question_answers(applicant):
            self._click_yes_no_question(frame, question_tokens, answer)

    def _fill_custom_answer(self, locator, field: ApplyField, answer: str) -> None:
        if self._is_numeric_field(field):
            numeric_answer = _numeric_only(answer)
            if not numeric_answer:
                raise RuntimeError(f"Numeric application field could not be answered safely: {field.label}")
            locator.fill(numeric_answer[:32], timeout=10000)
            return
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

        self._click_yes_no_question(
            frame,
            (
                "authorized to work lawfully in the united states",
                "authorized to work in the united states",
                "legally authorized to work in the united states",
            ),
            authorized,
        )

    def _click_yes_no_question(self, frame, question_tokens: tuple[str, ...], answer: bool) -> bool:
        desired_index = 0 if answer else 1
        field_entry = None
        for token in question_tokens:
            candidate = frame.locator("div.ashby-application-form-field-entry").filter(
                has=frame.locator("label.ashby-application-form-question-title", has_text=token)
            )
            if candidate.count() > 0:
                field_entry = candidate
                break
        if field_entry is None:
            return False
        if field_entry.count() == 0:
            return False

        buttons = field_entry.first.locator("div._yesno_17tft_149 button")
        if buttons.count() < 2:
            buttons = field_entry.first.locator("button")
        if buttons.count() < 2:
            return False

        desired_button = buttons.nth(desired_index)
        desired_button.click(timeout=5000)
        frame.wait_for_timeout(250)
        active_class = desired_button.get_attribute("class") or ""
        if "_active_y2cw4_58" not in active_class:
            raise RuntimeError(f"Unable to set Yes/No field for question tokens: {question_tokens}")
        return True

    def _is_custom_answer_field(self, field: ApplyField) -> bool:
        field_type = (field.type or "").lower()
        if field.tag not in {"input", "textarea"}:
            return False
        if field_type in {"hidden", "file", "radio", "checkbox", "email", "tel", "url"}:
            return False
        label = normalized_text(field.label)
        if label.startswith("if you answered") or label.startswith("if yes"):
            return False
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

    def _select_team_size_radio(
        self,
        frame,
        fields: list[ApplyField],
        applicant: ApplicantProfileConfig,
        resume_text: str,
    ) -> bool:
        options: dict[str, ApplyField] = {}
        for field in fields:
            if (field.type or "").lower() != "radio":
                continue
            option_label = _choice_option_label(field.label)
            if option_label in {"1-3 engineers", "4-5 engineers", "6-8 engineers", "9+ engineers"}:
                options[option_label] = field
        if len(options) < 4:
            return False
        if any(frame.locator(field.selector).first.is_checked() for field in options.values()):
            return True

        selected_option = _infer_team_size_option(applicant, resume_text)
        target = options.get(selected_option)
        if target is None:
            return False
        frame.locator(target.selector).first.check(force=True, timeout=5000)
        return True

    def _select_technology_checkboxes(
        self,
        frame,
        fields: list[ApplyField],
        resume_text: str,
    ) -> bool:
        options: dict[str, ApplyField] = {}
        for field in fields:
            if (field.type or "").lower() != "checkbox":
                continue
            option_label = _choice_option_label(field.label)
            if option_label in {"TypeScript", "Node.js", "React", "GraphQL", "PostgreSQL", "AWS", "Other (please specify)"}:
                options[option_label] = field
        if not options:
            return False

        matches = _technology_option_matches(resume_text)
        for option_label, field in options.items():
            if option_label == "Other (please specify)":
                continue
            if option_label not in matches:
                continue
            locator = frame.locator(field.selector).first
            if not locator.is_checked():
                locator.check(force=True, timeout=5000)
        return True

    def _known_custom_answer(self, field: ApplyField, applicant: ApplicantProfileConfig) -> str:
        label = normalized_text(field.label)
        if self._is_numeric_field(field):
            if "year" in label and "experience" in label:
                return _numeric_only(applicant.years_of_experience)
            if "compensation" in label or "salary" in label:
                return _digits_only(applicant.compensation_expectation)
            return ""
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
        if self._is_numeric_field(field):
            return "Return digits only as a whole number. Do not include words, commas, currency symbols, or punctuation."
        if field.tag == "textarea":
            return "Answer in 2-5 concise sentences unless the question asks for a list."
        return "Answer in one concise sentence or phrase."

    def _binary_question_answers(self, applicant: ApplicantProfileConfig) -> list[tuple[tuple[str, ...], bool]]:
        return [
            (
                (
                    "worked directly with product managers",
                    "define and deliver against a roadmap",
                ),
                True,
            ),
            (
                (
                    "employment-based visa sponsorship",
                    "require employment-based visa sponsorship",
                    "will you now or in the future require sponsorship",
                    "require sponsorship",
                ),
                applicant.requires_sponsorship,
            ),
            (("Are you currently in a period of Optimal Practical Training", "Optimal Practical Training"), False),
            (
                ("24-month OPT extension", "eligible for a 24-month OPT extension", "currently in OPT"),
                False,
            ),
            (("Are you a current APFM employee?", "current APFM employee"), False),
            (("Were you referred by a current A Place for Mom employee?", "referred by a current A Place for Mom employee"), False),
        ]

    def _is_numeric_field(self, field: ApplyField) -> bool:
        return (field.type or "").lower() == "number"

    def _check_cancelled(self, cancel_checker) -> None:
        if cancel_checker is not None and cancel_checker():
            raise RuntimeError("Apply agent cancelled.")

    def _record_step(self, steps: list[str], message: str) -> None:
        steps.append(message)
        logger.info("Ashby apply step: %s", message)


def _digits_only(value: str) -> str:
    digits = re.sub(r"[^\d]", "", value or "")
    return digits


def _numeric_only(value: str) -> str:
    match = re.search(r"\d+(?:\.\d+)?", value or "")
    return match.group(0) if match else ""


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


def _choice_option_label(label: str) -> str:
    return (label or "").split("|", 1)[0].strip()


def _infer_team_size_option(applicant: ApplicantProfileConfig, resume_text: str) -> str:
    lowered_resume = (resume_text or "").lower()
    max_team_size = 0
    patterns = [
        r"team of (\d+)\+?\s+(?:software\s+)?engineers",
        r"team of (\d+)-(\d+)\s+(?:onsite\s+)?engineers",
        r"managed a team of (\d+)-(\d+)",
        r"led a team of (\d+)\+?",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, lowered_resume):
            numbers = [int(value) for value in match.groups() if value and value.isdigit()]
            if numbers:
                max_team_size = max(max_team_size, max(numbers))

    if max_team_size >= 9:
        return "9+ engineers"
    if max_team_size >= 6:
        return "6-8 engineers"
    if max_team_size >= 4:
        return "4-5 engineers"
    if max_team_size >= 1:
        return "1-3 engineers"

    years = int(_digits_only(applicant.years_of_experience or "0") or "0")
    if years >= 15:
        return "6-8 engineers"
    if years >= 8:
        return "4-5 engineers"
    return "1-3 engineers"


def _technology_option_matches(resume_text: str) -> set[str]:
    text = normalized_text(resume_text or "")
    matches: set[str] = set()
    keywords = {
        "TypeScript": ("typescript",),
        "Node.js": ("node.js", "node js", " node "),
        "React": ("react",),
        "GraphQL": ("graphql",),
        "PostgreSQL": ("postgresql", "postgres"),
        "AWS": ("aws", "amazon web services"),
    }
    for option, terms in keywords.items():
        if any(term in text for term in terms):
            matches.add(option)
    return matches


def _extract_ashby_job_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query_job_id = parse_qs(parsed.query).get("ashby_jid", [""])[0]
    if query_job_id:
        return query_job_id
    match = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$|\?)", parsed.path, re.IGNORECASE)
    return match.group(1) if match else ""
