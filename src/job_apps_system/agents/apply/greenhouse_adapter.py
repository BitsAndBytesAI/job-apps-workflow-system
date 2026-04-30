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
from job_apps_system.services.application_answer_service import (
    ApplicationAnswerService,
    infer_structured_choice_candidates,
)


logger = logging.getLogger(__name__)
MAX_GREENHOUSE_SUBMIT_ATTEMPTS = 4

SUCCESS_TERMS = (
    "thank you for applying",
    "thank you for your application",
    "your application has been submitted",
    "we have received your application",
    "application submitted",
    "application has been submitted",
    "application was successfully submitted",
)

MANUAL_COMPLETION_MESSAGE = (
    "AI Job Agents filled out this application but cannot complete submission due to manual reCAPTCHA completion in this browser window."
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
        site_credential=None,
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

            self._fill_core_fields(frame, applicant, fields)
            self._record_step(steps, "Filled Greenhouse core applicant fields.")

            self._upload_resume(frame, resume_path)
            self._record_step(steps, "Uploaded resume PDF.")

            self._fill_known_questions(frame, fields, applicant, job, answer_service)
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

            has_manual_verification_gate = self._has_manual_verification_gate(frame)
            submit_result = self._submit_with_recovery(
                page=page,
                frame=frame,
                job=job,
                applicant=applicant,
                resume_path=resume_path,
                answer_service=answer_service,
                screenshot_path=screenshot_path,
                cancel_checker=cancel_checker,
                steps=steps,
                has_manual_verification_gate=has_manual_verification_gate,
            )
            if isinstance(submit_result, ApplyJobResult):
                return submit_result
            confirmation_text = submit_result
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

    def _fill_core_fields(self, frame, applicant: ApplicantProfileConfig, fields: list[ApplyField] | None = None) -> None:
        first_name, last_name = _split_name(applicant)
        self._fill_if_present(frame, "#first_name", first_name)
        self._fill_if_present(frame, "#last_name", last_name)
        self._fill_if_present(frame, "#email", applicant.email)
        self._fill_if_present(frame, "#phone", applicant.phone)
        self._select_combobox_value(frame, "#country", applicant.country, exact=False)
        self._fill_location_field(frame, "#candidate-location", applicant)

        available_fields = fields or []
        self._fill_matching_text_field(available_fields, frame, ("legal first name",), first_name)
        self._fill_matching_text_field(available_fields, frame, ("legal last name",), last_name)
        self._fill_matching_text_field(available_fields, frame, ("preferred name",), applicant.preferred_name or first_name)

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
            self._wait_for_resume_upload(frame, candidate, resume_path)
            return
        raise RuntimeError("Greenhouse resume upload field was not found.")

    def _fill_known_questions(
        self,
        frame,
        fields: list[ApplyField],
        applicant: ApplicantProfileConfig,
        job: Job,
        answer_service: ApplicationAnswerService,
    ) -> None:
        self._fill_matching_field(frame, fields, "linkedin profile", applicant.linkedin_url)
        self._fill_matching_field(frame, fields, "website", applicant.portfolio_url or applicant.github_url)
        self._select_matching_combobox(
            frame,
            fields,
            "require sponsorship",
            "Yes" if applicant.requires_sponsorship else "No",
        )
        self._select_matching_combobox(
            frame,
            fields,
            "immigration-related support or sponsorship",
            "Yes" if applicant.requires_sponsorship else "No",
        )

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

        self._fill_location_preference_options(frame, fields, applicant, job)

        for field in fields:
            if self._field_has_value(frame, field.selector):
                continue
            candidates = self._known_combobox_candidates(field, applicant)
            if candidates:
                if self._select_choice_field(frame, field, candidates):
                    continue
                if field.required:
                    answer_service.record_unanswered_field(
                        job=job,
                        field=field,
                        ats_type=self.ats_type,
                        reason="combobox_option_not_found",
                    )
                continue
            if not self._is_combobox_field(field):
                continue
            if field.required:
                answer_service.record_unanswered_field(
                    job=job,
                    field=field,
                    ats_type=self.ats_type,
                    reason="combobox_no_rule",
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
            if self._known_combobox_candidates(field, applicant):
                continue
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
                logger.info(
                    "Greenhouse custom answer field. selector=%s label=%s",
                    field.selector,
                    field.label,
                )
                locator.fill(answer[:3000], timeout=10000)
            elif not used_llm:
                answer_service.record_unanswered_field(
                    job=job,
                    field=field,
                    ats_type=self.ats_type,
                    reason="blank_after_inference",
                )

    def _submit(self, frame) -> None:
        button = frame.locator("button[type='submit']")
        if button.count() == 0:
            button = frame.get_by_role("button", name=re.compile(r"submit application", re.IGNORECASE))
        if button.count() == 0:
            raise RuntimeError("Greenhouse submit button was not found.")
        button.first.click(timeout=10000)
        try:
            frame.page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            frame.wait_for_timeout(5000)

    def _submit_with_recovery(
        self,
        *,
        page: Page,
        frame,
        job: Job,
        applicant: ApplicantProfileConfig,
        resume_path: Path,
        answer_service: ApplicationAnswerService,
        screenshot_path: Path,
        cancel_checker,
        steps: list[str],
        has_manual_verification_gate: bool,
    ) -> str | ApplyJobResult:
        last_exc: RuntimeError | None = None
        for attempt in range(1, MAX_GREENHOUSE_SUBMIT_ATTEMPTS + 1):
            self._check_cancelled(cancel_checker)
            self._submit(frame)
            if attempt == 1:
                self._record_step(steps, "Submitted Greenhouse application.")
            else:
                self._record_step(
                    steps,
                    f"Resubmitted Greenhouse application (attempt {attempt}/{MAX_GREENHOUSE_SUBMIT_ATTEMPTS}).",
                )
            try:
                return self._verify_success(page, frame)
            except RuntimeError as exc:
                last_exc = exc
                validation_errors = self._visible_validation_errors(frame)
                if validation_errors:
                    self._record_step(
                        steps,
                        "Greenhouse validation errors detected: " + " | ".join(validation_errors[:4]),
                    )
                    if attempt < MAX_GREENHOUSE_SUBMIT_ATTEMPTS:
                        self._recover_from_validation_errors(
                            frame=frame,
                            job=job,
                            applicant=applicant,
                            resume_path=resume_path,
                            answer_service=answer_service,
                            cancel_checker=cancel_checker,
                            steps=steps,
                            validation_errors=validation_errors,
                        )
                        continue
                if has_manual_verification_gate and self._should_use_manual_completion_fallback(exc):
                    self._record_step(steps, "Manual completion required; submit appears blocked by reCAPTCHA verification.")
                    return self._await_manual_completion(
                        page=page,
                        frame=frame,
                        job=job,
                        screenshot_path=screenshot_path,
                        cancel_checker=cancel_checker,
                        steps=steps,
                    )
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Greenhouse submit did not reach a terminal state.")

    def _verify_success(self, page: Page, frame) -> str:
        deadline_ms = 6000
        elapsed = 0
        last_body_texts: list[str] = []
        while elapsed <= deadline_ms:
            body_texts = self._post_submit_text_candidates(page, frame)
            if body_texts:
                last_body_texts = body_texts
                for body_text in body_texts:
                    if self._has_success_text(body_text):
                        return _first_confirmation_line(body_text)

            validation_errors = self._visible_validation_errors(frame)
            if validation_errors:
                raise RuntimeError(
                    "Greenhouse submit did not complete; visible field validation remains: "
                    + " | ".join(validation_errors[:5])
                )

            if elapsed >= deadline_ms:
                break

            try:
                page.wait_for_timeout(500)
            except PlaywrightError:
                break
            elapsed += 500

        body_texts = last_body_texts or self._post_submit_text_candidates(page, frame)
        if not body_texts:
            raise RuntimeError("Unable to read Greenhouse post-submit confirmation state.")

        excerpt = _body_excerpt(" ".join(body_texts))
        normalized = normalized_text(" ".join(body_texts))
        if "captcha" in normalized or "recaptcha" in normalized:
            raise RuntimeError(f"Application submit appears blocked by CAPTCHA. Visible text: {excerpt}")
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

    def _field_has_value(self, frame, selector: str) -> bool:
        locator = frame.locator(selector)
        if locator.count() == 0:
            return False
        try:
            return bool(locator.first.input_value(timeout=1000).strip())
        except PlaywrightError:
            return False

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

    def _fill_matching_text_field(
        self,
        fields: list[ApplyField],
        frame,
        keywords: tuple[str, ...],
        value: str | None,
    ) -> bool:
        if not value:
            return False
        for field in fields:
            label = normalized_text(_question_text(field.label))
            if not label or not all(keyword in label for keyword in keywords):
                continue
            if field.tag not in {"input", "textarea"}:
                continue
            if self._is_combobox_field(field):
                continue
            field_type = (field.type or "").lower()
            if field_type in {"hidden", "file", "radio", "checkbox"}:
                continue
            self._fill_field_selector(frame, field.selector, value)
            return True
        return False

    def _fill_location_field(self, frame, selector: str, applicant: ApplicantProfileConfig) -> bool:
        candidates = self._location_value_candidates(applicant)
        for candidate in candidates:
            if self._select_combobox_value(frame, selector, candidate, exact=False):
                return True
        if candidates:
            return self._fill_if_present(frame, selector, candidates[0])
        return False

    def _location_value_candidates(self, applicant: ApplicantProfileConfig) -> list[str]:
        candidates: list[str] = []
        city = applicant.city.strip()
        state = applicant.state.strip()
        country = applicant.country.strip()
        state_abbreviation = _state_abbreviation(state)

        if applicant.location_summary.strip():
            candidates.append(applicant.location_summary.strip())
        if city and state:
            candidates.append(f"{city}, {state}")
        if city and state_abbreviation:
            candidates.append(f"{city}, {state_abbreviation}")
        if city and country:
            candidates.append(f"{city}, {country}")
        if city:
            candidates.append(city)
        if applicant.full_address.strip():
            candidates.append(applicant.full_address.strip())

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = normalized_text(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

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

    def _select_combobox_option(self, frame, selector: str, candidates: list[str]) -> bool:
        if not candidates:
            return False
        locator = frame.locator(selector)
        if locator.count() == 0:
            return False
        field = locator.first
        field.click(timeout=8000, force=True)
        frame.wait_for_timeout(400)

        options = frame.get_by_role("option")
        if options.count() == 0:
            return False
        option_texts = [text.strip() for text in options.all_inner_texts() if text and text.strip()]
        normalized_options = {normalized_text(text): text for text in option_texts}
        matched_text: str | None = None
        for candidate in candidates:
            normalized_candidate = normalized_text(candidate)
            if normalized_candidate in normalized_options:
                matched_text = normalized_options[normalized_candidate]
                break
            for option_text in option_texts:
                normalized_option = normalized_text(option_text)
                if normalized_candidate in normalized_option or normalized_option in normalized_candidate:
                    matched_text = option_text
                    break
            if matched_text:
                break
        if not matched_text:
            frame.press("body", "Escape")
            return False

        frame.get_by_role("option", name=re.compile(rf"^{re.escape(matched_text)}$", re.IGNORECASE)).first.click(
            timeout=5000,
            force=True,
        )
        frame.wait_for_timeout(300)
        return True

    def _select_choice_field(self, frame, field: ApplyField, candidates: list[str]) -> bool:
        if not candidates:
            return False
        if field.tag == "select":
            locator = frame.locator(field.selector)
            if locator.count() == 0:
                return False
            for candidate in candidates:
                try:
                    locator.first.select_option(label=candidate, timeout=5000)
                    return True
                except PlaywrightError:
                    try:
                        locator.first.select_option(candidate, timeout=5000)
                        return True
                    except PlaywrightError:
                        continue
            return False
        for candidate in candidates:
            if self._select_combobox_value(frame, field.selector, candidate, exact=True):
                return True
            if self._select_combobox_value(frame, field.selector, candidate, exact=False):
                return True
        return False

    def _is_combobox_field(self, field: ApplyField) -> bool:
        if field.tag != "input" or (field.type or "").lower() != "text":
            return False
        return any(_is_select_placeholder(part) for part in _label_parts(field.label))

    def _fill_location_preference_options(
        self,
        frame,
        fields: list[ApplyField],
        applicant: ApplicantProfileConfig,
        job: Job,
    ) -> bool:
        location_fields = [
            field
            for field in fields
            if field.tag == "input" and (field.type or "").lower() == "checkbox" and _looks_like_location_option(field.label)
        ]
        if not location_fields:
            return False
        if any(frame.locator(field.selector).first.is_checked() for field in location_fields):
            return True

        for candidate in self._location_preference_candidates(applicant, job):
            matched = next(
                (
                    field
                    for field in location_fields
                    if normalized_text(_primary_label_text(field.label)) == normalized_text(candidate)
                ),
                None,
            )
            if matched is None:
                continue
            locator = frame.locator(matched.selector).first
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.check(timeout=8000, force=True)
            self._normalize_required_checkbox_group(frame, locator)
            frame.wait_for_timeout(200)
            return True
        return False

    def _location_preference_candidates(self, applicant: ApplicantProfileConfig, job: Job) -> list[str]:
        candidates: list[str] = []
        job_text = normalized_text(" ".join(part for part in [job.job_title or "", job.job_description or ""] if part))
        if "remote" in job_text:
            candidates.append("Remote")

        location_summary = normalized_text(applicant.location_summary)
        country = normalized_text(applicant.country)
        state = normalized_text(applicant.state)
        city = normalized_text(applicant.city)

        if "texas" in location_summary or state == "tx" or state == "texas" or city == "austin":
            candidates.append("Austin, TX")
        if "california" in location_summary or state == "ca" or state == "california" or city == "san mateo":
            candidates.append("San Mateo, CA")
        if "ohio" in location_summary or state == "oh" or state == "ohio" or city == "columbus":
            candidates.append("Columbus, OH")
        if "united states" in country or country == "us" or country == "usa":
            candidates.append("Remote")

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = normalized_text(candidate)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(candidate)
        return deduped

    def _is_custom_answer_field(self, field: ApplyField) -> bool:
        if self._is_combobox_field(field):
            return False
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

    def _known_combobox_candidates(self, field: ApplyField, applicant: ApplicantProfileConfig) -> list[str]:
        return infer_structured_choice_candidates(field.label, applicant)

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

    def _await_manual_completion(
        self,
        *,
        page: Page,
        frame,
        job: Job,
        screenshot_path: Path,
        cancel_checker,
        steps: list[str],
    ) -> ApplyJobResult:
        self._show_manual_completion_overlay(page)
        self._record_step(steps, "Displayed manual completion instructions in the browser window.")
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass

        while True:
            if page.is_closed():
                break
            self._check_cancelled(cancel_checker)
            try:
                confirmation_text = self._manual_completion_success_text(page, frame)
            except PlaywrightTimeoutError:
                confirmation_text = ""
            except PlaywrightError:
                if page.is_closed():
                    break
                confirmation_text = ""
            if confirmation_text:
                try:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception:
                    pass
                self._record_step(steps, f"Detected manual submission success: {confirmation_text}")
                try:
                    page.close()
                except PlaywrightError:
                    pass
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
            try:
                page.wait_for_timeout(1000)
            except PlaywrightError:
                if page.is_closed():
                    break
                raise

        self._record_step(steps, "Manual completion browser window closed.")
        return ApplyJobResult(
            job_id=job.id,
            company_name=job.company_name,
            job_title=job.job_title,
            ats_type=self.ats_type,
            status="manual_closed",
            success=False,
            screenshot_path=str(screenshot_path) if screenshot_path.exists() else None,
            confirmation_text=MANUAL_COMPLETION_MESSAGE,
            steps=steps,
        )

    def _manual_completion_success_text(self, page: Page, frame) -> str:
        for body_text in self._post_submit_text_candidates(page, frame):
            if self._has_success_text(body_text):
                return _first_confirmation_line(body_text)
        return ""

    def _post_submit_text_candidates(self, page: Page, frame) -> list[str]:
        text_candidates: list[str] = []
        try:
            text_candidates.append(frame.locator("body").inner_text(timeout=2000))
        except PlaywrightError:
            pass
        try:
            text_candidates.append(page.locator("body").inner_text(timeout=2000))
        except PlaywrightError:
            pass
        try:
            for candidate_frame in page.frames:
                frame_url = (candidate_frame.url or "").lower()
                if candidate_frame == frame or _is_greenhouse_frame_url(frame_url) or frame_url.startswith("about:blank"):
                    try:
                        text_candidates.append(candidate_frame.locator("body").inner_text(timeout=1000))
                    except PlaywrightError:
                        continue
        except PlaywrightError:
            pass
        return [text for text in text_candidates if isinstance(text, str) and text.strip()]

    def _should_use_manual_completion_fallback(self, exc: RuntimeError) -> bool:
        message = normalized_text(str(exc))
        if "visible field validation remains" in message:
            return False
        if "captcha" in message or "recaptcha" in message:
            return True
        if "could not verify successful greenhouse application submission" in message:
            return True
        if "unable to read greenhouse post-submit confirmation state" in message:
            return True
        return False

    def _recover_from_validation_errors(
        self,
        *,
        frame,
        job: Job,
        applicant: ApplicantProfileConfig,
        resume_path: Path,
        answer_service: ApplicationAnswerService,
        cancel_checker,
        steps: list[str],
        validation_errors: list[str],
    ) -> None:
        fields = extract_apply_fields(frame, frame_id="greenhouse")
        invalid_fields = self._invalid_fields(frame, fields)
        if invalid_fields:
            invalid_labels = ", ".join(
                _primary_label_text(field.label) or field.selector for field in invalid_fields[:4]
            )
            self._record_step(
                steps,
                f"Retrying {len(invalid_fields)} invalid field(s): {invalid_labels}.",
            )
        else:
            self._record_step(steps, f"Retrying after validation errors with {len(fields)} visible field(s).")

        if self._errors_reference_resume(validation_errors) or any(
            "resume" in normalized_text(field.label) for field in invalid_fields
        ):
            self._upload_resume(frame, resume_path)
            self._record_step(steps, "Re-uploaded resume after validation error.")

        recovered_count = 0
        if invalid_fields:
            for field in invalid_fields:
                self._check_cancelled(cancel_checker)
                if self._recover_invalid_field(frame, field, applicant, job, answer_service):
                    recovered_count += 1
        else:
            self._fill_core_fields(frame, applicant, fields)
            self._fill_known_questions(frame, fields, applicant, job, answer_service)
            self._answer_custom_fields(frame, fields, applicant, job, answer_service, cancel_checker)
            recovered_count = len(fields)

        self._record_step(
            steps,
            f"Refilled {recovered_count} Greenhouse invalid field(s) after validation errors."
            if invalid_fields
            else "Refilled Greenhouse fields after validation errors.",
        )

    def _invalid_fields(self, frame, fields: list[ApplyField]) -> list[ApplyField]:
        invalid_ids = frame.locator("[data-apply-agent-id]").evaluate_all(
            """
            els => els
              .filter((el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0 || style.display === "none" || style.visibility === "hidden" || el.disabled) {
                  return false;
                }
                const ariaInvalid = (el.getAttribute("aria-invalid") || "").toLowerCase() === "true";
                let nativeInvalid = false;
                try {
                  nativeInvalid = el.matches(":invalid");
                } catch (error) {
                  nativeInvalid = false;
                }
                return ariaInvalid || nativeInvalid;
              })
              .map((el) => el.getAttribute("data-apply-agent-id"))
              .filter(Boolean)
            """
        )
        by_id = {field.element_id: field for field in fields}
        invalid_fields: list[ApplyField] = []
        seen: set[str] = set()
        for element_id in invalid_ids:
            field = by_id.get(element_id)
            if field is None or field.element_id in seen:
                continue
            seen.add(field.element_id)
            invalid_fields.append(field)
        return invalid_fields

    def _recover_invalid_field(
        self,
        frame,
        field: ApplyField,
        applicant: ApplicantProfileConfig,
        job: Job,
        answer_service: ApplicationAnswerService,
    ) -> bool:
        first_name, last_name = _split_name(applicant)
        label = normalized_text(_question_text(field.label))

        if "legal first name" in label or ("first name" in label and "preferred" not in label):
            self._fill_field_selector(frame, field.selector, first_name)
            return True
        if "legal last name" in label or ("last name" in label and "preferred" not in label):
            self._fill_field_selector(frame, field.selector, last_name)
            return True
        if "preferred name" in label:
            self._fill_field_selector(frame, field.selector, applicant.preferred_name or first_name)
            return True
        if "email" in label:
            self._fill_field_selector(frame, field.selector, applicant.email)
            return True
        if "phone" in label:
            self._fill_field_selector(frame, field.selector, applicant.phone)
            return True
        if "location" in label:
            return self._fill_location_field(frame, field.selector, applicant)
        if "country" in label:
            return self._select_combobox_value(frame, field.selector, applicant.country, exact=False)

        candidates = self._known_combobox_candidates(field, applicant)
        if not candidates and "location" in label:
            candidates = self._location_value_candidates(applicant)
        if candidates:
            return self._select_choice_field(frame, field, candidates)

        if self._is_combobox_field(field):
            return False

        if not self._is_custom_answer_field(field):
            return False

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
            self._fill_field_selector(frame, field.selector, answer[:3000])
            return True
        if not used_llm:
            answer_service.record_unanswered_field(
                job=job,
                field=field,
                ats_type=self.ats_type,
                reason="blank_after_inference",
            )
        return False

    def _errors_reference_resume(self, validation_errors: list[str]) -> bool:
        return any("resume" in normalized_text(message) for message in validation_errors)

    def _has_manual_verification_gate(self, frame) -> bool:
        try:
            return bool(
                frame.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('iframe'))
                      .map((frame) => frame.src || '')
                      .some((src) => /captcha|recaptcha|turnstile/i.test(src))
                    """
                )
            )
        except PlaywrightError:
            return False

    def _show_manual_completion_overlay(self, page: Page) -> None:
        try:
            page.evaluate(
                """
                ({ title, message, buttonLabel }) => {
                  const existing = document.getElementById('__apply-agent-manual-modal__');
                  if (existing) existing.remove();
                  const existingStyle = document.getElementById('__apply-agent-manual-modal-style__');
                  if (existingStyle) existingStyle.remove();
                  const overlay = document.createElement('div');
                  overlay.id = '__apply-agent-manual-modal__';
                  overlay.innerHTML = `
                    <div class="apply-agent-manual-overlay" data-apply-agent-manual>
                      <div class="apply-agent-manual-card" role="dialog" aria-modal="true" aria-labelledby="apply-agent-manual-title">
                        <span class="apply-agent-manual-kicker">AI Job Agents</span>
                        <h2 id="apply-agent-manual-title">${title}</h2>
                        <p>${message}</p>
                        <button type="button" id="apply-agent-manual-dismiss">${buttonLabel}</button>
                      </div>
                    </div>
                  `;
                  const style = document.createElement('style');
                  style.id = '__apply-agent-manual-modal-style__';
                  style.textContent = `
                    @import url("https://api.fontshare.com/v2/css?f[]=satoshi@400,500,700&display=swap");
                    @import url("https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500&display=swap");
                    .apply-agent-manual-overlay {
                      position: fixed;
                      inset: 0;
                      z-index: 2147483647;
                      background: rgba(27, 28, 30, 0.56);
                      display: flex;
                      align-items: center;
                      justify-content: center;
                      padding: 24px;
                    }
                    .apply-agent-manual-card {
                      position: relative;
                      max-width: 600px;
                      width: min(100%, 600px);
                      background: #f5f3ee;
                      color: #1b1c1e;
                      border: 1px solid rgba(27, 28, 30, 0.12);
                      border-radius: 28px;
                      box-shadow: 0 24px 80px rgba(27, 28, 30, 0.18);
                      padding: 30px 32px 28px;
                      font-family: "Satoshi", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
                    }
                    .apply-agent-manual-card::before {
                      content: "";
                      position: absolute;
                      inset: 0;
                      border-radius: inherit;
                      pointer-events: none;
                      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.45);
                    }
                    .apply-agent-manual-kicker {
                      display: inline-flex;
                      align-items: center;
                      margin-bottom: 12px;
                      padding: 8px 14px;
                      border-radius: 999px;
                      background: rgba(30, 58, 138, 0.1);
                      color: #15296b;
                      font-size: 12px;
                      font-weight: 700;
                      letter-spacing: 0.08em;
                      text-transform: uppercase;
                    }
                    .apply-agent-manual-card h2 {
                      margin: 0 0 14px;
                      color: #1b1c1e;
                      font-family: "Fraunces", Georgia, serif;
                      font-size: 42px;
                      line-height: 1.02;
                      letter-spacing: -0.03em;
                    }
                    .apply-agent-manual-card p {
                      margin: 0 0 24px;
                      color: rgba(27, 28, 30, 0.88);
                      font-size: 20px;
                      line-height: 1.45;
                      letter-spacing: -0.01em;
                    }
                    #apply-agent-manual-dismiss {
                      appearance: none;
                      border: 1px solid rgba(27, 28, 30, 0.12);
                      border-radius: 999px;
                      background: #1e3a8a;
                      color: #ffffff;
                      font-family: "Satoshi", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
                      font-size: 16px;
                      font-weight: 700;
                      line-height: 1;
                      padding: 15px 22px;
                      cursor: pointer;
                      transition: background 180ms ease, transform 180ms ease;
                    }
                    #apply-agent-manual-dismiss:hover {
                      background: #15296b;
                      transform: translateY(-1px);
                    }
                  `;
                  const cleanup = () => {
                    overlay.remove();
                    style.remove();
                  };
                  overlay.querySelector('#apply-agent-manual-dismiss')?.addEventListener('click', cleanup);
                  document.head.appendChild(style);
                  document.body.appendChild(overlay);
                }
                """,
                {
                    "title": "reCAPTCHA detected - Complete Form Manually",
                    "message": MANUAL_COMPLETION_MESSAGE,
                    "buttonLabel": "Continue Manual Submit",
                },
            )
        except PlaywrightError:
            return

    def _wait_for_resume_upload(self, frame, locator, resume_path: Path) -> None:
        page = frame.page
        try:
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and "grnhse-prod-jben-us-east-1.s3.amazonaws.com" in response.url,
                timeout=15000,
            ):
                locator.set_input_files(str(resume_path), timeout=10000)
        except PlaywrightTimeoutError:
            locator.set_input_files(str(resume_path), timeout=10000)
        try:
            frame.get_by_text(resume_path.name, exact=False).first.wait_for(timeout=8000)
        except PlaywrightTimeoutError:
            frame.wait_for_timeout(1500)

    def _visible_validation_errors(self, frame) -> list[str]:
        selectors = (
            ".field-error",
            "[aria-invalid='true']",
            "[id$='-error']",
        )
        messages: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            locator = frame.locator(selector)
            count = locator.count()
            for index in range(count):
                try:
                    item = locator.nth(index)
                    if not item.is_visible():
                        continue
                    text = " ".join(item.inner_text(timeout=500).split()).strip()
                except PlaywrightError:
                    continue
                if not text:
                    continue
                normalized_message = normalized_text(text)
                if normalized_message in {"required", "select...", "select"}:
                    continue
                if normalized_message in seen:
                    continue
                seen.add(normalized_message)
                messages.append(text)
        return messages

    def _normalize_required_checkbox_group(self, frame, locator) -> None:
        try:
            name = locator.get_attribute("name")
        except PlaywrightError:
            return
        if not name:
            return
        try:
            frame.evaluate(
                """
                ({ name }) => {
                  const boxes = Array.from(document.querySelectorAll(`input[type="checkbox"][name="${CSS.escape(name)}"]`));
                  if (!boxes.some((box) => box.checked)) return;
                  for (const box of boxes) {
                    box.required = false;
                    box.removeAttribute("required");
                  }
                }
                """,
                {"name": name},
            )
        except PlaywrightError:
            return


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


def _label_parts(label: str) -> list[str]:
    return [part.strip() for part in (label or "").split("|") if part.strip()]


def _is_select_placeholder(value: str) -> bool:
    return normalized_text(value) in {"select", "select..."}


def _question_text(label: str) -> str:
    parts = [part for part in _label_parts(label) if not _is_select_placeholder(part)]
    return " | ".join(parts)


def _looks_like_location_option(label: str) -> bool:
    normalized = normalized_text(_primary_label_text(label))
    if not normalized:
        return False
    return normalized in {"remote", "austin, tx", "san mateo, ca", "columbus, oh"}


def _primary_label_text(label: str) -> str:
    parts = [part for part in _label_parts(label) if not _is_select_placeholder(part)]
    return parts[0] if parts else ""


US_STATE_ABBREVIATIONS = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def _state_abbreviation(state: str | None) -> str:
    cleaned = (state or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return US_STATE_ABBREVIATIONS.get(normalized_text(cleaned), "")
