from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from job_apps_system.agents.apply.action_map import extract_apply_fields, normalized_text
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.schemas.apply import ApplyAction, ApplyField, ApplyJobResult
from job_apps_system.services.application_answer_service import ApplicationAnswerService
from job_apps_system.services.apply_site_sessions import ApplySiteCredential


logger = logging.getLogger(__name__)

AI_BROWSER_ATS_TYPE = "ai_browser"
MAX_AI_TURNS = 20
MAX_ACTIONS_PER_TURN = 5
MAX_TOTAL_ACTIONS = 60
MAX_SUBMIT_ATTEMPTS = 4
MAX_RUNTIME_SECONDS = 8 * 60
MAX_REPEATED_ACTIONS = 2
MAX_ENTRY_CLICKS = 3
MAX_AUTH_ACTIONS = 14
MANUAL_RESUME_TIMEOUT_SECONDS = 30 * 60
POST_SUBMIT_PROCESSING_WAIT_SECONDS = 75
ACTION_LOG_LIMIT = 80
PASSWORD_PLACEHOLDER = "__APPLY_SITE_PASSWORD__"
EMAIL_PLACEHOLDER = "__APPLY_SITE_EMAIL__"
APPLY_AGENT_OVERLAY_SELECTOR = (
    "#__apply-agent-activity-overlay__, "
    "#__apply-agent-activity-style__, "
    "#__apply-agent-manual-modal__, "
    "#__apply-agent-manual-dock__"
)

ALLOWED_ACTIONS = {
    "fill",
    "select",
    "click",
    "upload_resume",
    "scroll",
    "wait",
    "screenshot",
    "submit_application",
    "stop_for_manual",
    "needs_review",
}

SUCCESS_TERMS = (
    "thank you for applying",
    "thank you for your application",
    "your application has been submitted",
    "we have received your application",
    "application submitted",
    "application has been submitted",
    "application was successfully submitted",
    "application has been received",
    "application is on its way",
    "your application is on its way",
    "fantastic your application is on its way",
)

SUBMIT_TERMS = (
    "submit",
    "submit application",
    "send application",
    "apply",
    "send",
)

APPLICATION_ENTRY_TERMS = (
    "apply",
    "apply now",
    "apply for this job",
    "apply to this job",
    "apply on company site",
    "apply on company website",
    "start application",
    "start your application",
    "begin application",
    "continue application",
    "continue applying",
    "continue to application",
    "easy apply",
)

NON_APPLICATION_ENTRY_TERMS = (
    "apply filter",
    "apply filters",
    "save",
    "saved",
    "job alert",
    "email alert",
)

HARD_STOP_TERMS = (
    "login_required",
    "password_field",
    "multi-factor",
    "two-factor",
    "verification code",
    "payment",
    "bank account",
    "credit card",
    "routing number",
    "social security number",
    "government id",
)

AUTH_BLOCKER_TERMS = {"login_required", "password_field"}

MANUAL_COMPLETION_MESSAGE = "Complete the login, verification, CAPTCHA, or blocked step in this browser window. Click Resume AI when ready, or choose to finish manually."
DICE_EXISTING_ACCOUNT_MESSAGE = (
    "Dice says this email already has an account. Log in in this browser window, then click Resume AI. "
    "The saved Dice browser profile will be reused for future Dice applications."
)

APPLICATION_FORM_FIELD_TERMS = (
    "first name",
    "given name",
    "last name",
    "family name",
    "surname",
    "full name",
    "legal name",
    "phone",
    "mobile",
    "address",
    "city",
    "state",
    "province",
    "zip",
    "postal",
    "country",
    "resume",
    "cv",
    "cover letter",
    "linkedin",
    "portfolio",
    "work authorization",
    "authorized to work",
    "sponsorship",
    "sponsor",
    "visa",
    "salary",
    "compensation",
    "veteran",
    "disability",
    "gender",
    "race",
    "ethnicity",
    "hispanic",
    "eeo",
    "voluntary self-identification",
    "experience",
    "employer",
    "education",
    "school",
    "degree",
)
AUTH_ONLY_FIELD_TERMS = (
    "email",
    "email address",
    "username",
    "user name",
    "password",
    "confirm password",
    "verification code",
    "one-time code",
    "captcha",
)
APPLICATION_FORM_PAGE_TERMS = (
    "job application",
    "apply online",
    "application form",
    "submit resume",
    "candidate profile",
    "my information",
    "work experience",
    "voluntary disclosures",
    "legally authorized",
    "authorized to work",
    "require sponsorship",
)


class AiBrowserApplyLoop:
    ats_type = AI_BROWSER_ATS_TYPE

    def __init__(self, *, retain_success_logs: bool = False) -> None:
        self._retain_success_logs = retain_success_logs
        self._action_log: list[dict[str, Any]] = []
        self._submit_attempts = 0
        self._total_actions = 0
        self._action_fingerprints: dict[str, int] = {}
        self._entry_click_fingerprints: set[str] = set()
        self._auth_action_fingerprints: set[str] = set()
        self._manual_resume_url: str | None = None
        self._last_submit_at: float | None = None
        self._post_submit_wait_logged = False

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
        detected_ats: str = "unknown",
        site_credential: ApplySiteCredential | None = None,
        initial_reason: str = "",
        manual_resume_url: str | None = None,
        cancel_checker=None,
        before_observe_hook=None,
    ) -> ApplyJobResult:
        steps: list[str] = []
        started_at = time.monotonic()
        self._current_page = page
        self._manual_resume_url = manual_resume_url
        self._record_step(steps, f"Started AI browser apply loop for {detected_ats or 'unknown'} page.")
        if initial_reason:
            self._record_step(steps, f"AI loop reason: {initial_reason}.")

        try:
            for turn in range(1, MAX_AI_TURNS + 1):
                self._check_cancelled(cancel_checker)
                if time.monotonic() - started_at > MAX_RUNTIME_SECONDS:
                    return self._ambiguous_result(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        message="AI browser loop reached the time limit.",
                    )

                confirmation_text = self._success_text(page)
                if confirmation_text:
                    self._remove_activity_overlay(page)
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    self._record_step(steps, f"Detected successful submission: {confirmation_text}")
                    return self._result(
                        job=job,
                        detected_ats=detected_ats,
                        status="submitted",
                        success=True,
                        screenshot_path=screenshot_path,
                        confirmation_text=confirmation_text,
                        steps=steps,
                    )

                self._show_activity_overlay(page)
                if before_observe_hook and before_observe_hook(page, steps):
                    continue
                observation = self._observe(page, detected_ats=detected_ats)
                auth_context = self._auth_context(site_credential)
                if auth_context:
                    observation["auth_context"] = auth_context
                if detected_ats == "dice_profile" and self._dice_existing_account_error_visible(page, observation):
                    if self._click_dice_existing_account_login(page, observation, steps):
                        continue
                    self._record_step(steps, "Dice says this email already has an account; manual login is needed.")
                    manual_result = self._await_manual_resolution(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                        message=DICE_EXISTING_ACCOUNT_MESSAGE,
                    )
                    if manual_result is None:
                        continue
                    return manual_result
                if self._should_yield_dice_profile_detour(page, observation):
                    message = "Dice profile or registration detour has no actionable form fields; returning control to the Dice adapter."
                    self._record_step(steps, message)
                    self._remove_activity_overlay(page)
                    return self._result(
                        job=job,
                        detected_ats=detected_ats,
                        status="needs_review",
                        success=False,
                        screenshot_path=screenshot_path,
                        confirmation_text=message,
                        steps=steps,
                    )
                blockers = observation.get("blockers") or []
                if self._dismiss_common_privacy_overlay(page, steps):
                    continue
                if self._has_auth_blocker(blockers):
                    if self._attempt_auth_or_registration(
                        page=page,
                        observation=observation,
                        applicant=applicant,
                        site_credential=site_credential,
                        steps=steps,
                    ):
                        continue
                    if site_credential is not None and self._auth_action_fingerprints:
                        self._record_step(steps, "Passing remaining auth or registration page to AI planner.")
                    else:
                        self._record_step(steps, "AI loop needs user help with login or registration.")
                        manual_result = self._await_manual_resolution(
                            page=page,
                            job=job,
                            detected_ats=detected_ats,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            cancel_checker=cancel_checker,
                            message=MANUAL_COMPLETION_MESSAGE,
                        )
                        if manual_result is None:
                            continue
                        return manual_result
                if "manual_verification" in blockers and not self._manual_verification_requires_handoff(observation):
                    observation = self._downgrade_actionable_manual_verification(observation)
                    blockers = observation.get("blockers") or []
                if "manual_verification" in blockers and self._manual_verification_requires_handoff(observation):
                    self._record_step(steps, "AI loop detected manual verification.")
                    manual_result = self._await_manual_resolution(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                        message=MANUAL_COMPLETION_MESSAGE,
                    )
                    if manual_result is None:
                        continue
                    return manual_result
                if self._has_non_auth_hard_stop(blockers):
                    self._record_step(steps, "AI loop detected a manual-only blocker.")
                    manual_result = self._await_manual_resolution(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                        message=MANUAL_COMPLETION_MESSAGE,
                    )
                    if manual_result is None:
                        continue
                    return manual_result
                if self._click_dice_review_submit_if_ready(
                    page=page,
                    observation=observation,
                    detected_ats=detected_ats,
                    resume_path=resume_path,
                    screenshot_path=screenshot_path,
                    auto_submit=auto_submit,
                    steps=steps,
                ):
                    continue
                if self._submit_attempts > 0 and self._post_submit_processing_in_progress(observation):
                    if self._should_wait_for_post_submit_processing():
                        if not self._post_submit_wait_logged:
                            self._record_step(steps, "Submit is processing; waiting for the confirmation page.")
                            self._post_submit_wait_logged = True
                        page.wait_for_timeout(3000)
                        continue
                if self._submit_attempts > 0 and self._has_manual_submit_blocker(observation):
                    self._record_step(steps, "AI loop detected a concrete post-submit blocker.")
                    manual_result = self._await_manual_resolution(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                        message=MANUAL_COMPLETION_MESSAGE,
                    )
                    if manual_result is None:
                        continue
                    return manual_result

                try:
                    clicked_entry = self._click_application_entry_if_present(
                        page=page,
                        observation=observation,
                        detected_ats=detected_ats,
                        resume_path=resume_path,
                        screenshot_path=screenshot_path,
                        auto_submit=auto_submit,
                        steps=steps,
                    )
                except ManualHandoffRequested as exc:
                    self._record_step(steps, str(exc))
                    manual_result = self._await_manual_resolution(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                        message=MANUAL_COMPLETION_MESSAGE,
                    )
                    if manual_result is None:
                        continue
                    return manual_result
                if clicked_entry:
                    continue

                plan = answer_service.generate_browser_action_plan(
                    observation=observation,
                    applicant=applicant,
                    job=job,
                    auto_submit=auto_submit,
                    auth_context=auth_context,
                    recent_actions=self._action_log,
                )
                self._record_step(steps, f"AI browser loop turn {turn}: {plan.summary or 'received action plan'}.")

                if plan.needs_manual:
                    self._record_step(steps, plan.summary or "AI loop requested manual completion.")
                    manual_result = self._await_manual_resolution(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                        message=plan.summary or MANUAL_COMPLETION_MESSAGE,
                    )
                    if manual_result is None:
                        continue
                    return manual_result
                if plan.needs_review or plan.terminal or plan.done:
                    self._remove_activity_overlay(page)
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    return self._result(
                        job=job,
                        detected_ats=detected_ats,
                        status="needs_review",
                        success=False,
                        screenshot_path=screenshot_path,
                        confirmation_text=plan.summary or plan.error or "AI browser loop stopped for review.",
                        steps=steps,
                    )

                actions = plan.actions[:MAX_ACTIONS_PER_TURN]
                if not actions:
                    self._remove_activity_overlay(page)
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    return self._result(
                        job=job,
                        detected_ats=detected_ats,
                        status="needs_review",
                        success=False,
                        screenshot_path=screenshot_path,
                        confirmation_text=plan.error or "AI browser loop did not return an executable action.",
                        steps=steps,
                    )

                target_map = self._target_map(observation)
                for action in actions:
                    self._check_cancelled(cancel_checker)
                    if self._total_actions >= MAX_TOTAL_ACTIONS:
                        return self._ambiguous_result(
                            page=page,
                            job=job,
                            detected_ats=detected_ats,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            message="AI browser loop reached the action limit.",
                        )
                    if self._is_repeated_action(action):
                        self._log_action(action, "skipped", "repeated_action_limit")
                        continue
                    try:
                        result = self._execute_action(
                            page=page,
                            action=action,
                            targets=target_map,
                            resume_path=resume_path,
                            screenshot_path=screenshot_path,
                            auto_submit=auto_submit,
                            site_credential=site_credential,
                        )
                    except ManualHandoffRequested as exc:
                        self._record_step(steps, str(exc))
                        manual_result = self._await_manual_resolution(
                            page=page,
                            job=job,
                            detected_ats=detected_ats,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            cancel_checker=cancel_checker,
                            message=str(exc) or MANUAL_COMPLETION_MESSAGE,
                        )
                        if manual_result is None:
                            continue
                        return manual_result
                    except NeedsReviewRequested as exc:
                        self._remove_activity_overlay(page)
                        page.screenshot(path=str(screenshot_path), full_page=True)
                        return self._result(
                            job=job,
                            detected_ats=detected_ats,
                            status="needs_review",
                            success=False,
                            screenshot_path=screenshot_path,
                            confirmation_text=str(exc),
                            steps=steps,
                        )
                    except AmbiguousSubmitState as exc:
                        return self._ambiguous_result(
                            page=page,
                            job=job,
                            detected_ats=detected_ats,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            message=str(exc),
                        )
                    self._total_actions += 1
                    self._log_action(action, result["status"], result["message"], result.get("target"))

                    confirmation_text = self._success_text(page)
                    if confirmation_text:
                        self._remove_activity_overlay(page)
                        page.screenshot(path=str(screenshot_path), full_page=True)
                        return self._result(
                            job=job,
                            detected_ats=detected_ats,
                            status="submitted",
                            success=True,
                            screenshot_path=screenshot_path,
                            confirmation_text=confirmation_text,
                            steps=steps,
                        )

            return self._ambiguous_result(
                page=page,
                job=job,
                detected_ats=detected_ats,
                screenshot_path=screenshot_path,
                steps=steps,
                message="AI browser loop reached the turn limit.",
            )
        except Exception as exc:
            if str(exc) == "Apply agent cancelled.":
                raise
            captured_path: str | None = None
            try:
                self._remove_activity_overlay(page)
                page.screenshot(path=str(screenshot_path), full_page=True)
                captured_path = str(screenshot_path)
            except Exception:
                pass
            self._record_step(steps, f"AI browser loop failed: {exc}")
            logger.exception("AI browser apply loop failed. job_id=%s", job.id)
            return ApplyJobResult(
                job_id=job.id,
                company_name=job.company_name,
                job_title=job.job_title,
                ats_type=detected_ats or self.ats_type,
                status="failed",
                success=False,
                error=str(exc),
                screenshot_path=captured_path,
                steps=steps,
                action_log=self._trimmed_action_log(),
            )

    def await_manual_resolution(
        self,
        *,
        page: Page,
        job: Job,
        detected_ats: str,
        screenshot_path: Path,
        steps: list[str],
        cancel_checker=None,
        message: str = MANUAL_COMPLETION_MESSAGE,
        manual_resume_url: str | None = None,
    ) -> ApplyJobResult | None:
        self._current_page = page
        self._manual_resume_url = manual_resume_url
        return self._await_manual_resolution(
            page=page,
            job=job,
            detected_ats=detected_ats,
            screenshot_path=screenshot_path,
            steps=steps,
            cancel_checker=cancel_checker,
            message=message,
        )

    def _execute_action(
        self,
        *,
        page: Page,
        action: ApplyAction,
        targets: dict[str, dict[str, Any]],
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
        site_credential: ApplySiteCredential | None = None,
    ) -> dict[str, Any]:
        action_type = normalized_text(action.action).replace(" ", "_")
        if action_type not in ALLOWED_ACTIONS:
            return {"status": "skipped", "message": "action_not_allowed"}

        if action_type == "wait":
            page.wait_for_timeout(1500)
            return {"status": "success", "message": "waited"}
        if action_type == "scroll":
            page.mouse.wheel(0, 700)
            return {"status": "success", "message": "scrolled"}
        if action_type == "screenshot":
            page.screenshot(path=str(screenshot_path), full_page=True)
            return {"status": "success", "message": "captured_screenshot"}
        if action_type == "stop_for_manual":
            raise ManualHandoffRequested("AI loop requested manual completion.")
        if action_type == "needs_review":
            raise NeedsReviewRequested("AI loop requested review.")

        target = targets.get(action.element_id or "")
        if target is None:
            return {"status": "skipped", "message": "unknown_element_id"}
        locator = target["frame"].locator(target["selector"]).first
        kind = target["kind"]

        if action_type == "upload_resume":
            if kind != "field" or target.get("type") != "file":
                return {"status": "skipped", "message": "upload_target_not_file", "target": target}
            locator.set_input_files(str(resume_path), timeout=10000)
            page.wait_for_timeout(1500)
            return {"status": "success", "message": "uploaded_resume", "target": target}

        if action_type == "fill":
            if kind != "field":
                return {"status": "skipped", "message": "fill_target_not_field", "target": target}
            raw_value = self._resolve_action_value(action.value, target, site_credential)
            value = self._coerce_field_value(raw_value, target)
            if self._should_preserve_existing_value(locator, target, replacement=value):
                return {"status": "skipped", "message": "preserved_existing_value", "target": target}
            if not value and target.get("required"):
                return {"status": "skipped", "message": "blank_required_value", "target": target}
            locator.fill(value[:3000], timeout=10000)
            return {"status": "success", "message": "filled", "target": target}

        if action_type == "select":
            if kind != "field":
                return {"status": "skipped", "message": "select_target_not_field", "target": target}
            return self._select_target(page, locator, target, action.value)

        if action_type == "click":
            if kind != "button":
                return {"status": "skipped", "message": "click_target_not_button", "target": target}
            if not self._is_visible_click_target(locator):
                if self._click_observed_button_fallback(target):
                    page.wait_for_timeout(1200)
                    return {"status": "success", "message": "clicked_by_observed_ordinal", "target": target}
                return {"status": "skipped", "message": "click_target_not_visible", "target": target}
            if self._should_require_submit_application_action(target, targets):
                return {"status": "skipped", "message": "submit_requires_submit_application_action", "target": target}
            if self._is_obviously_unrelated_navigation(page, target):
                return {"status": "skipped", "message": "unrelated_navigation_blocked", "target": target}
            try:
                locator.click(timeout=10000)
            except PlaywrightTimeoutError:
                if self._click_observed_button_fallback(target):
                    page.wait_for_timeout(1200)
                    return {"status": "success", "message": "clicked_by_observed_ordinal", "target": target}
                return {"status": "skipped", "message": "click_target_not_actionable", "target": target}
            page.wait_for_timeout(1200)
            return {"status": "success", "message": "clicked", "target": target}

        if action_type == "submit_application":
            if kind == "button" and self._is_application_entry_target(target, targets):
                if not self._is_visible_click_target(locator):
                    return {"status": "skipped", "message": "click_target_not_visible", "target": target}
                if self._is_obviously_unrelated_navigation(page, target):
                    return {"status": "skipped", "message": "unrelated_navigation_blocked", "target": target}
                try:
                    locator.click(timeout=10000)
                except PlaywrightTimeoutError:
                    if self._click_observed_button_fallback(target):
                        page.wait_for_timeout(1200)
                        return {"status": "success", "message": "clicked_application_entry_by_observed_ordinal", "target": target}
                    return {"status": "skipped", "message": "click_target_not_actionable", "target": target}
                page.wait_for_timeout(1200)
                return {"status": "success", "message": "clicked_application_entry", "target": target}
            if not auto_submit:
                raise NeedsReviewRequested("Auto-submit is disabled.")
            if kind != "button" or not self._is_submit_target(target):
                return {"status": "skipped", "message": "submit_target_not_final_submit", "target": target}
            if not self._is_visible_click_target(locator):
                return {"status": "skipped", "message": "submit_target_not_visible", "target": target}
            if str(target.get("tag") or "").lower() == "a" and str(target.get("href") or "").strip():
                return {"status": "skipped", "message": "submit_target_is_navigation", "target": target}
            errors = self._visible_validation_errors(target["frame"])
            if errors:
                return {"status": "skipped", "message": "validation_errors_present", "target": target}
            self._submit_attempts += 1
            if self._submit_attempts > MAX_SUBMIT_ATTEMPTS:
                raise AmbiguousSubmitState("Submit attempt limit reached.")
            try:
                locator.click(timeout=10000)
            except PlaywrightTimeoutError:
                if self._click_observed_button_fallback(target):
                    self._last_submit_at = time.monotonic()
                    self._post_submit_wait_logged = False
                    page.wait_for_timeout(1200)
                    return {"status": "success", "message": f"submitted_attempt_{self._submit_attempts}", "target": target}
                return {"status": "skipped", "message": "submit_target_not_actionable", "target": target}
            self._last_submit_at = time.monotonic()
            self._post_submit_wait_logged = False
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(3000)
            return {"status": "success", "message": f"submitted_attempt_{self._submit_attempts}", "target": target}

        return {"status": "skipped", "message": "unhandled_action"}

    def _select_target(self, page: Page, locator, target: dict[str, Any], value: str | bool | None) -> dict[str, Any]:
        field_type = (target.get("type") or "").lower()
        tag = (target.get("tag") or "").lower()
        if field_type == "checkbox":
            expected = _coerce_checkbox_value(value, default=True)
            if not self._set_checkable_target(locator, target, checked=expected):
                return {"status": "failed", "message": "checkbox_selection_failed", "target": target}
            return {"status": "success", "message": "selected_checkbox", "target": target}
        if field_type == "radio":
            if not self._set_checkable_target(locator, target, checked=True):
                return {"status": "failed", "message": "radio_selection_failed", "target": target}
            return {"status": "success", "message": "selected_radio", "target": target}
        if tag == "select":
            text = str(value or "")
            for candidate in (text, text.strip()):
                if not candidate:
                    continue
                try:
                    locator.select_option(label=candidate, timeout=5000)
                    return {"status": "success", "message": "selected_option", "target": target}
                except PlaywrightError:
                    try:
                        locator.select_option(candidate, timeout=5000)
                        return {"status": "success", "message": "selected_option", "target": target}
                    except PlaywrightError:
                        continue
            return {"status": "failed", "message": "select_option_not_found", "target": target}

        text = str(value or "").strip()
        if not text:
            return {"status": "skipped", "message": "blank_select_value", "target": target}
        locator.click(timeout=8000, force=True)
        try:
            locator.fill("", timeout=3000)
            locator.fill(text, timeout=8000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(700)
        option = self._match_option(target["frame"], text)
        if option is None:
            return {"status": "failed", "message": "combobox_option_not_found", "target": target}
        option.click(timeout=5000, force=True)
        return {"status": "success", "message": "selected_combobox_option", "target": target}

    def _set_checkable_target(self, locator, target: dict[str, Any], *, checked: bool) -> bool:
        try:
            if checked:
                locator.check(timeout=8000, force=True)
            else:
                locator.uncheck(timeout=8000, force=True)
            if self._checkable_state_matches(locator, checked):
                return True
        except PlaywrightError:
            pass

        frame = target.get("frame")
        selector = str(target.get("selector") or "").strip()
        if not frame or not selector:
            return False
        try:
            return bool(
                frame.evaluate(
                    """
                    ({ selector, checked }) => {
                      const root = document.querySelector(selector);
                      if (!root) return false;
                      const control = root.matches('input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"]')
                        ? root
                        : root.querySelector('input[type="checkbox"], input[type="radio"], [role="checkbox"], [role="radio"]');
                      if (!control) return false;
                      const readState = (node) => {
                        if ('checked' in node) return Boolean(node.checked);
                        return String(node.getAttribute('aria-checked') || '').toLowerCase() === 'true';
                      };
                      const dispatch = (node) => {
                        node.dispatchEvent(new Event('input', { bubbles: true }));
                        node.dispatchEvent(new Event('change', { bubbles: true }));
                      };
                      const writeState = (node, expected) => {
                        if ('checked' in node) {
                          const proto = Object.getPrototypeOf(node);
                          const descriptor = Object.getOwnPropertyDescriptor(proto, 'checked');
                          if (descriptor && descriptor.set) {
                            descriptor.set.call(node, expected);
                          } else {
                            node.checked = expected;
                          }
                        }
                        node.setAttribute('aria-checked', expected ? 'true' : 'false');
                        dispatch(node);
                      };
                      const cssEscape = (value) => (
                        window.CSS && CSS.escape
                          ? CSS.escape(value)
                          : String(value).replace(/["\\\\]/g, '\\\\$&')
                      );
                      const clickTargets = [];
                      if (control.labels) clickTargets.push(...Array.from(control.labels));
                      if (control.id) {
                        const label = document.querySelector(`label[for="${cssEscape(control.id)}"]`);
                        if (label) clickTargets.push(label);
                      }
                      const closestLabel = control.closest('label');
                      if (closestLabel) clickTargets.push(closestLabel);
                      const roleControl = control.closest('[role="checkbox"], [role="radio"]');
                      if (roleControl) clickTargets.push(roleControl);
                      clickTargets.push(control);
                      if (readState(control) !== checked) {
                        for (const clickTarget of clickTargets) {
                          try {
                            clickTarget.click();
                            break;
                          } catch (_) {}
                        }
                      }
                      if (readState(control) !== checked) {
                        writeState(control, checked);
                      }
                      if (readState(control) !== checked && root !== control) {
                        writeState(root, checked);
                      }
                      return readState(control) === checked || readState(root) === checked;
                    }
                    """,
                    {"selector": selector, "checked": checked},
                )
            )
        except PlaywrightError:
            return False

    @staticmethod
    def _checkable_state_matches(locator, checked: bool) -> bool:
        try:
            return bool(locator.is_checked(timeout=1000)) is checked
        except PlaywrightError:
            return False

    def _click_observed_button_fallback(self, target: dict[str, Any]) -> bool:
        frame = target.get("frame")
        if not frame:
            return False
        text = normalized_text(target.get("text") or "")
        ordinal = target.get("ordinal")
        if ordinal is None:
            match = re.search(r"btn_(\d+)", str(target.get("id") or ""))
            ordinal = int(match.group(1)) - 1 if match else None
        try:
            ordinal = int(ordinal)
        except (TypeError, ValueError):
            return False
        if ordinal < 0:
            return False
        try:
            return bool(
                frame.evaluate(
                    """
                    ({ ordinal, text }) => {
                      const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        if (!el || !(el instanceof Element)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 &&
                          rect.height > 0 &&
                          style.display !== 'none' &&
                          style.visibility !== 'hidden' &&
                          style.opacity !== '0' &&
                          !el.disabled &&
                          el.getAttribute('aria-disabled') !== 'true';
                      };
                      const buttonText = (el) => normalize(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '');
                      const controls = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"], a'))
                        .filter(isVisible);
                      const target = controls[ordinal];
                      if (!target || (text && buttonText(target) !== text)) return false;
                      target.scrollIntoView({ block: 'center', inline: 'center' });
                      target.click();
                      return true;
                    }
                    """,
                    {"ordinal": ordinal, "text": text},
                )
            )
        except PlaywrightError:
            return False

    def _is_visible_click_target(self, locator) -> bool:
        try:
            locator.scroll_into_view_if_needed(timeout=1000)
        except PlaywrightError:
            pass
        try:
            return bool(locator.is_visible(timeout=500))
        except PlaywrightError:
            return False

    def _click_dice_review_submit_if_ready(
        self,
        *,
        page: Page,
        observation: dict[str, Any],
        detected_ats: str,
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
        steps: list[str],
    ) -> bool:
        if detected_ats != "dice" or not auto_submit:
            return False
        parsed = urlparse(str(observation.get("page_url") or page.url or ""))
        host = parsed.netloc.lower()
        if host != "dice.com" and not host.endswith(".dice.com"):
            return False
        if not parsed.path.lower().startswith("/job-applications/"):
            return False

        page_text = normalized_text(self._safe_body_text(page))
        if "review your application" not in page_text:
            return False
        if "work authorization" not in page_text:
            return False
        for frame in observation.get("frames", []):
            if frame.get("validation_errors"):
                return False

        targets = self._target_map(observation)
        candidates = [
            target
            for target in targets.values()
            if target.get("kind") == "button"
            and not target.get("disabled")
            and self._is_submit_target(target)
            and not self._is_application_entry_target(target, targets)
        ]
        if not candidates:
            return False

        candidates.sort(key=_submit_button_priority)
        target = candidates[0]
        action = ApplyAction(
            action="submit_application",
            element_id=str(target.get("id") or ""),
            reasoning="Dice final review page is complete; submit the application without another LLM turn.",
            confidence=0.98,
        )
        try:
            result = self._execute_action(
                page=page,
                action=action,
                targets=targets,
                resume_path=resume_path,
                screenshot_path=screenshot_path,
                auto_submit=auto_submit,
            )
        except (ManualHandoffRequested, NeedsReviewRequested, AmbiguousSubmitState):
            raise
        except PlaywrightError:
            return False

        self._total_actions += 1
        self._log_action(action, result["status"], result["message"], result.get("target"))
        if result["status"] != "success":
            return False
        self._record_step(steps, "Submitted Dice final review page.")
        return True

    def _click_application_entry_if_present(
        self,
        *,
        page: Page,
        observation: dict[str, Any],
        detected_ats: str,
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
        steps: list[str],
    ) -> bool:
        if len(self._entry_click_fingerprints) >= MAX_ENTRY_CLICKS or self._total_actions >= MAX_TOTAL_ACTIONS:
            return False

        targets = self._target_map(observation)
        target = self._select_application_entry_target(page, detected_ats, targets)
        if target is None:
            return False

        fingerprint = self._entry_target_fingerprint(page, target)
        if fingerprint in self._entry_click_fingerprints:
            return False
        self._entry_click_fingerprints.add(fingerprint)

        action = ApplyAction(
            action="click",
            element_id=str(target.get("id") or ""),
            reasoning="Deterministically advance a pre-application entry CTA before LLM planning.",
            confidence=1.0,
        )
        try:
            result = self._execute_action(
                page=page,
                action=action,
                targets=targets,
                resume_path=resume_path,
                screenshot_path=screenshot_path,
                auto_submit=auto_submit,
            )
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            self._total_actions += 1
            message = "entry_click_failed"
            if self._has_auth_gate_on_page(page):
                message = "entry_click_blocked_by_auth_gate"
            self._log_action(action, "failed", message, target)
            if message == "entry_click_blocked_by_auth_gate":
                raise ManualHandoffRequested("Application entry is blocked by a login or join modal.") from exc
            raise
        self._total_actions += 1
        self._log_action(action, result["status"], result["message"], result.get("target"))

        label = str(target.get("text") or target.get("label") or "application entry").strip()
        if result["status"] == "success":
            self._record_step(steps, f"Clicked application entry button: {label}.")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1500)
            return True

        self._record_step(steps, f"Application entry button was not clicked: {label} ({result['message']}).")
        return False

    def _observe(self, page: Page, *, detected_ats: str) -> dict[str, Any]:
        frames: list[dict[str, Any]] = []
        for frame_index, frame in enumerate(page.frames):
            frame_id = f"frame_{frame_index}"
            try:
                fields = extract_apply_fields(frame, frame_id=frame_id)
            except PlaywrightError:
                fields = []
            observed_fields = []
            for field in fields:
                target_id = f"{frame_id}:{field.element_id}"
                value_info = self._field_value_info(frame, field)
                target = {
                    "id": target_id,
                    "kind": "field",
                    "frame_id": frame_id,
                    "selector": field.selector,
                    "label": field.label,
                    "tag": field.tag,
                    "type": field.type or "",
                    "required": field.required,
                    "placeholder": field.placeholder,
                    **value_info,
                }
                observed_fields.append(_public_target(target))

            buttons = self._observe_buttons(frame, frame_id=frame_id)

            frames.append(
                {
                    "frame_id": frame_id,
                    "url": frame.url,
                    "fields": observed_fields,
                    "buttons": [_public_target(button) for button in buttons],
                    "validation_errors": self._visible_validation_errors(frame),
                    "blockers": self._detect_blockers(frame),
                }
            )
        page_text = self._safe_body_text(page)
        return {
            "page_url": page.url,
            "detected_ats": detected_ats,
            "frames": frames,
            "blockers": self._page_blockers(frames, page_text),
            "success_text": self._success_text(page),
        }

    def _target_map(self, observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
        # Re-observe to get live frame/locator handles after the model call.
        # The model only sees stable observed ids; executor resolves fresh handles.
        live = self._observe_from_current_page_handles(observation)
        return live

    def _observe_from_current_page_handles(self, observation: dict[str, Any]) -> dict[str, dict[str, Any]]:
        targets: dict[str, dict[str, Any]] = {}
        for frame_info in observation.get("frames", []):
            frame = self._frame_by_id(observation, frame_info.get("frame_id"))
            if frame is None:
                continue
            for item in list(frame_info.get("fields", [])) + list(frame_info.get("buttons", [])):
                target_id = str(item.get("id") or "")
                if not target_id:
                    continue
                selector = self._selector_for_observed_target(item)
                if not selector:
                    continue
                targets[target_id] = {"frame": frame, **item, "selector": selector}
        return targets

    def _frame_by_id(self, observation: dict[str, Any], frame_id: str | None):
        # The observation dict stores frames in page.frames order. It is valid
        # only for the current page state and is refreshed every model turn.
        match = re.fullmatch(r"frame_(\d+)", str(frame_id or ""))
        if not match:
            return None
        index = int(match.group(1))
        page = self._current_page
        if index < 0 or index >= len(page.frames):
            return None
        return page.frames[index]

    @property
    def _current_page(self) -> Page:
        if not hasattr(self, "__current_page"):
            raise RuntimeError("AI browser loop page context was not initialized.")
        return getattr(self, "__current_page")

    @_current_page.setter
    def _current_page(self, page: Page) -> None:
        setattr(self, "__current_page", page)

    def _field_value_info(self, frame, field: ApplyField) -> dict[str, Any]:
        locator = frame.locator(field.selector).first
        value = ""
        checked = None
        field_type = (field.type or "").lower()
        try:
            if field_type in {"checkbox", "radio"}:
                checked = locator.is_checked()
            else:
                value = locator.input_value(timeout=500)
        except PlaywrightError:
            value = ""
        invalid = False
        try:
            invalid = bool(
                locator.evaluate(
                    "(el) => (el.getAttribute('aria-invalid') || '').toLowerCase() === 'true' || el.matches(':invalid')"
                )
            )
        except PlaywrightError:
            invalid = False
        public_value = "[password set]" if field_type == "password" and str(value).strip() else str(value)[:120]
        return {
            "has_value": bool(str(value).strip()) or checked is True,
            "current_value": public_value,
            "checked": checked,
            "invalid": invalid,
        }

    def _observe_buttons(self, frame, *, frame_id: str) -> list[dict[str, Any]]:
        try:
            rows = frame.locator("button, [role='button'], input[type='button'], input[type='submit'], a").evaluate_all(
                """
                els => els
                  .filter((el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  })
                  .slice(0, 80)
                  .map((el, index) => {
                    const id = `btn_${String(index + 1).padStart(3, '0')}`;
                    el.setAttribute('data-apply-agent-button-id', id);
                    const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                    const text = normalize(el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '').slice(0, 180);
                    const labels = [];
                    const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
                    for (const itemId of labelledBy) {
                      const item = document.getElementById(itemId);
                      if (item) labels.push(normalize(item.innerText || item.textContent || ''));
                    }
                    const describedBy = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
                    for (const itemId of describedBy) {
                      const item = document.getElementById(itemId);
                      if (item) labels.push(normalize(item.innerText || item.textContent || ''));
                    }
                    let parent = el.parentElement;
                    for (let depth = 0; parent && depth < 5; depth += 1, parent = parent.parentElement) {
                      const parentText = normalize(parent.innerText || parent.textContent || '');
                      if (parentText && parentText !== text && parentText.length <= 900) {
                        labels.push(parentText);
                        break;
                      }
                    }
                    const context = labels.filter(Boolean).join(' | ').slice(0, 500);
                    return {
                      element_id: id,
                      text,
                      label: context,
                      ordinal: index,
                      tag: el.tagName.toLowerCase(),
                      href: el.getAttribute('href') || '',
                      selector: `[data-apply-agent-button-id="${id}"]`,
                      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                      busy: Boolean(el.getAttribute('aria-busy') === 'true' || el.className?.toString().toLowerCase().includes('loading') || el.className?.toString().toLowerCase().includes('spinner')),
                    };
                  })
                """
            )
        except PlaywrightError:
            rows = []
        buttons: list[dict[str, Any]] = []
        for row in rows:
            target_id = f"{frame_id}:{row['element_id']}"
            buttons.append(
                {
                    "id": target_id,
                    "kind": "button",
                    "frame_id": frame_id,
                    "selector": row["selector"],
                    "text": row.get("text") or "",
                    "label": row.get("label") or "",
                    "ordinal": row.get("ordinal"),
                    "tag": row.get("tag") or "",
                    "href": row.get("href") or "",
                    "disabled": bool(row.get("disabled")),
                    "busy": bool(row.get("busy")),
                }
            )
        return buttons

    def _visible_validation_errors(self, frame) -> list[str]:
        selectors = (".field-error", "[aria-invalid='true']", "[id$='-error']", ".error", ".validation-error")
        messages: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            locator = frame.locator(selector)
            try:
                count = min(locator.count(), 30)
            except PlaywrightError:
                continue
            for index in range(count):
                try:
                    item = locator.nth(index)
                    if not item.is_visible():
                        continue
                    text = " ".join(item.inner_text(timeout=500).split()).strip()
                except PlaywrightError:
                    continue
                normalized = normalized_text(text)
                if not normalized or normalized in seen or normalized in {"required", "select", "select..."}:
                    continue
                seen.add(normalized)
                messages.append(text[:240])
        return messages

    def _detect_blockers(self, frame) -> list[str]:
        try:
            payload = frame.evaluate(
                """
                (overlaySelector) => {
                  const bodyClone = document.body?.cloneNode(true);
                  bodyClone?.querySelectorAll(overlaySelector).forEach((el) => el.remove());
                  const text = ((bodyClone || document.body)?.innerText || '').toLowerCase();
                  const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  const visiblePassword = Array.from(document.querySelectorAll('input[type="password"]'))
                    .some((el) => {
                      const style = window.getComputedStyle(el);
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled;
                    });
                  const visibleCaptchaText = Array.from(document.querySelectorAll('[id*="captcha" i], [class*="captcha" i], [id*="turnstile" i], [class*="turnstile" i], .g-recaptcha, [data-sitekey]'))
                    .filter(isVisible)
                    .map((el) => [
                      el.innerText || el.textContent || '',
                      el.getAttribute('id') || '',
                      el.getAttribute('class') || '',
                      el.getAttribute('aria-label') || '',
                    ].join(' '))
                    .join('\\n');
                  const visibleCaptchaFrameSources = Array.from(document.querySelectorAll('iframe'))
                    .filter(isVisible)
                    .map((item) => item.src || '')
                    .filter((src) => /captcha|recaptcha|hcaptcha|turnstile|challenges\\.cloudflare/i.test(src));
                  const visibleDialogText = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], .modal, .modal__overlay, .top-level-modal-container'))
                    .filter(isVisible)
                    .map((el) => (el.innerText || el.textContent || '').toLowerCase())
                    .join('\\n');
                  return { text, visiblePassword, visibleCaptchaText, visibleCaptchaFrameSources, visibleDialogText };
                }
                """,
                APPLY_AGENT_OVERLAY_SELECTOR,
            )
        except PlaywrightError:
            return []
        text = normalized_text(str(payload.get("text") or ""))
        visible_captcha_frame_sources = " ".join(
            str(item).lower() for item in payload.get("visibleCaptchaFrameSources") or []
        )
        visible_captcha = " ".join(
            [
                str(payload.get("visibleCaptchaText") or ""),
                visible_captcha_frame_sources,
            ]
        )
        blockers: list[str] = []
        if _looks_like_active_manual_verification(visible_captcha, iframe_sources=visible_captcha_frame_sources):
            blockers.append("manual_verification")
        if payload.get("visiblePassword"):
            blockers.append("password_field")
        if _looks_like_auth_gate_text(str(payload.get("visibleDialogText") or "")):
            blockers.append("login_required")
        for token in HARD_STOP_TERMS:
            if token in text:
                blockers.append(token)
        return sorted(set(blockers))

    def _page_blockers(self, frames: list[dict[str, Any]], page_text: str) -> list[str]:
        blockers: set[str] = set()
        for frame in frames:
            blockers.update(frame.get("blockers") or [])
        normalized = normalized_text(page_text)
        if _looks_like_auth_gate_text(normalized):
            blockers.add("login_required")
        for token in HARD_STOP_TERMS:
            if token in normalized:
                blockers.add(token)
        return sorted(blockers)

    def _success_text(self, page: Page) -> str:
        try:
            page_url = normalized_text(page.url)
            page_title = normalized_text(page.title())
        except PlaywrightError:
            page_url = ""
            page_title = ""
        if "/success" in page_url or "application success" in page_title:
            for text in self._body_text_candidates(page):
                normalized = normalized_text(text)
                if "application" in normalized or "fantastic" in normalized:
                    return _first_confirmation_line(text)
            return "Application submitted."
        for text in self._body_text_candidates(page):
            normalized = normalized_text(text)
            if any(term in normalized for term in SUCCESS_TERMS):
                return _first_confirmation_line(text)
        return ""

    def _body_text_candidates(self, page: Page) -> list[str]:
        texts: list[str] = []
        try:
            texts.append(
                page.evaluate(
                    """
                    (overlaySelector) => {
                      const bodyClone = document.body?.cloneNode(true);
                      bodyClone?.querySelectorAll(overlaySelector).forEach((el) => el.remove());
                      return (bodyClone || document.body)?.innerText || '';
                    }
                    """,
                    APPLY_AGENT_OVERLAY_SELECTOR,
                )
            )
        except PlaywrightError:
            pass
        for frame in page.frames:
            try:
                texts.append(
                    frame.evaluate(
                        """
                        (overlaySelector) => {
                          const bodyClone = document.body?.cloneNode(true);
                          bodyClone?.querySelectorAll(overlaySelector).forEach((el) => el.remove());
                          return (bodyClone || document.body)?.innerText || '';
                        }
                        """,
                        APPLY_AGENT_OVERLAY_SELECTOR,
                    )
                )
            except PlaywrightError:
                continue
        return [text for text in texts if isinstance(text, str) and text.strip()]

    def _safe_body_text(self, page: Page) -> str:
        texts = self._body_text_candidates(page)
        compact = " ".join(" ".join(text.split()) for text in texts)
        return compact[:1200]

    def _has_auth_gate_on_page(self, page: Page) -> bool:
        return any(_looks_like_auth_gate_text(text) for text in self._body_text_candidates(page))

    def _auth_context(self, site_credential: ApplySiteCredential | None) -> dict[str, Any] | None:
        if site_credential is None:
            return None
        return {
            "credentials_available": True,
            "site_key": site_credential.site_key,
            "email": site_credential.email,
            "password_placeholder": PASSWORD_PLACEHOLDER,
            "email_placeholder": EMAIL_PLACEHOLDER,
            "instruction": "Use the password placeholder for password and confirm-password fields. The executor replaces it with the stored keychain password.",
        }

    def _has_auth_blocker(self, blockers: list[str]) -> bool:
        return any(blocker in AUTH_BLOCKER_TERMS for blocker in blockers)

    def _has_non_auth_hard_stop(self, blockers: list[str]) -> bool:
        return any(blocker in HARD_STOP_TERMS and blocker not in AUTH_BLOCKER_TERMS for blocker in blockers)

    def _has_hard_stop(self, blockers: list[str]) -> bool:
        return any(blocker in HARD_STOP_TERMS for blocker in blockers)

    def _attempt_auth_or_registration(
        self,
        *,
        page: Page,
        observation: dict[str, Any],
        applicant: ApplicantProfileConfig,
        site_credential: ApplySiteCredential | None,
        steps: list[str],
    ) -> bool:
        if site_credential is None or len(self._auth_action_fingerprints) >= MAX_AUTH_ACTIONS:
            return False

        targets = self._target_map(observation)
        field_actions = self._auth_field_actions(targets, applicant, site_credential)
        button_target = self._select_auth_button_target(targets)
        action_fingerprint = self._auth_fingerprint(page, field_actions, button_target)
        if action_fingerprint in self._auth_action_fingerprints:
            return False
        if not field_actions and button_target is None:
            return False

        self._auth_action_fingerprints.add(action_fingerprint)
        did_work = False
        for action in field_actions:
            if self._total_actions >= MAX_TOTAL_ACTIONS:
                break
            result = self._execute_action(
                page=page,
                action=action,
                targets=targets,
                resume_path=Path(),
                screenshot_path=Path(),
                auto_submit=False,
                site_credential=site_credential,
            )
            self._total_actions += 1
            self._log_action(action, result["status"], result["message"], result.get("target"))
            did_work = did_work or result["status"] == "success"

        if button_target is not None and self._total_actions < MAX_TOTAL_ACTIONS:
            action = ApplyAction(
                action="click",
                element_id=str(button_target.get("id") or ""),
                reasoning="Advance login or account creation after filling generated site credentials.",
                confidence=0.92,
            )
            result = self._execute_action(
                page=page,
                action=action,
                targets=targets,
                resume_path=Path(),
                screenshot_path=Path(),
                auto_submit=False,
                site_credential=site_credential,
            )
            self._total_actions += 1
            self._log_action(action, result["status"], result["message"], result.get("target"))
            did_work = did_work or result["status"] == "success"

        if did_work:
            self._record_step(steps, "Tried login/account creation with the generated site credential.")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1800)
        return did_work

    def _auth_field_actions(
        self,
        targets: dict[str, dict[str, Any]],
        applicant: ApplicantProfileConfig,
        site_credential: ApplySiteCredential,
    ) -> list[ApplyAction]:
        actions: list[ApplyAction] = []
        first_name, last_name = _split_name(applicant.legal_name or applicant.preferred_name)
        for target in targets.values():
            if target.get("kind") != "field" or target.get("disabled"):
                continue
            field_type = str(target.get("type") or "").lower()
            if field_type in {"hidden", "checkbox", "radio", "file", "submit", "button"}:
                continue
            if target.get("has_value") and not target.get("invalid"):
                continue
            label = _field_label(target)
            value: str | None = None
            if self._is_password_field(target):
                value = PASSWORD_PLACEHOLDER
            elif self._is_email_or_username_field(target):
                value = site_credential.email
            elif _looks_like_first_name_field(label):
                value = first_name
            elif _looks_like_last_name_field(label):
                value = last_name
            elif _looks_like_full_name_field(label):
                value = applicant.legal_name or applicant.preferred_name
            if value:
                actions.append(
                    ApplyAction(
                        action="fill",
                        element_id=str(target.get("id") or ""),
                        value=value,
                        reasoning="Fill login or registration field using the applicant profile and generated site credential.",
                        confidence=0.95,
                    )
                )
        return actions[:6]

    def _select_auth_button_target(self, targets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        candidates = []
        prefer_login = _auth_targets_prefer_login(targets)
        for target in targets.values():
            if target.get("kind") != "button" or target.get("disabled"):
                continue
            text = normalized_text(target.get("text") or target.get("label") or "")
            if not text or _is_sso_button_text(text):
                continue
            if _is_auth_button_text(text):
                candidates.append(target)
        if not candidates:
            return None
        candidates.sort(key=lambda target: _auth_button_priority(target, prefer_login=prefer_login))
        return candidates[0]

    def _is_password_field(self, target: dict[str, Any]) -> bool:
        field_type = str(target.get("type") or "").lower()
        return field_type == "password" or "password" in _field_label(target)

    def _is_email_or_username_field(self, target: dict[str, Any]) -> bool:
        field_type = str(target.get("type") or "").lower()
        label = _field_label(target)
        return field_type == "email" or "email" in label or "username" in label or "user name" in label

    @staticmethod
    def _auth_fingerprint(
        page: Page,
        field_actions: list[ApplyAction],
        button_target: dict[str, Any] | None,
    ) -> str:
        action_ids = ",".join(str(action.element_id or "") for action in field_actions)
        button_id = str((button_target or {}).get("id") or "")
        parsed = urlparse(page.url)
        return "|".join([parsed.netloc.lower(), parsed.path.lower(), action_ids, button_id])

    def _has_manual_submit_blocker(self, observation: dict[str, Any]) -> bool:
        blockers = set(observation.get("blockers") or [])
        if "manual_verification" in blockers:
            return True
        for frame in observation.get("frames", []):
            errors = frame.get("validation_errors") or []
            if errors:
                return False
        return False

    def _post_submit_processing_in_progress(self, observation: dict[str, Any]) -> bool:
        for frame in observation.get("frames", []):
            if frame.get("validation_errors"):
                return False
            for button in frame.get("buttons", []):
                if not self._is_submit_target(button) or self._is_application_entry_target(button):
                    continue
                text = normalized_text(" ".join(str(button.get(key) or "") for key in ("text", "label")))
                if button.get("disabled") or button.get("busy"):
                    return True
                if any(term in text for term in ("submitting", "loading", "please wait", "processing")):
                    return True
        return False

    def _should_wait_for_post_submit_processing(self) -> bool:
        if self._last_submit_at is None:
            return False
        return time.monotonic() - self._last_submit_at < POST_SUBMIT_PROCESSING_WAIT_SECONDS

    def _manual_verification_requires_handoff(self, observation: dict[str, Any]) -> bool:
        if self._submit_attempts > 0:
            return True
        for frame in observation.get("frames", []):
            visible_fields = [
                field
                for field in frame.get("fields", [])
                if str(field.get("type") or "").lower() not in {"hidden", "search"}
            ]
            visible_buttons = [
                button
                for button in frame.get("buttons", [])
                if not button.get("disabled") and not self._is_manual_verification_button(button)
            ]
            if visible_fields or visible_buttons:
                return False
        return True

    @staticmethod
    def _is_manual_verification_button(button: dict[str, Any]) -> bool:
        text = normalized_text(button.get("text") or button.get("label") or "")
        return any(
            token in text
            for token in (
                "verify",
                "captcha",
                "i'm not a robot",
                "im not a robot",
                "i am human",
                "i'm human",
            )
        )

    @staticmethod
    def _downgrade_actionable_manual_verification(observation: dict[str, Any]) -> dict[str, Any]:
        cleaned = dict(observation)
        cleaned["blockers"] = [blocker for blocker in observation.get("blockers", []) if blocker != "manual_verification"]
        cleaned["manual_verification_present_but_actionable"] = True
        cleaned["manual_verification_note"] = (
            "A verification artifact is visible, but application fields or buttons are still actionable. "
            "Continue filling the application and stop for manual help only if verification blocks progress."
        )
        frames = []
        for frame in observation.get("frames", []):
            frame_copy = dict(frame)
            frame_copy["blockers"] = [blocker for blocker in frame.get("blockers", []) if blocker != "manual_verification"]
            frames.append(frame_copy)
        cleaned["frames"] = frames
        return cleaned

    def _dismiss_common_privacy_overlay(self, page: Page, steps: list[str]) -> bool:
        for frame in page.frames:
            try:
                clicked_label = frame.evaluate(
                    """
                    () => {
                      const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 &&
                          rect.height > 0 &&
                          style.display !== 'none' &&
                          style.visibility !== 'hidden' &&
                          style.opacity !== '0';
                      };
                      const containers = Array.from(document.querySelectorAll([
                        '[role="dialog"]',
                        '[aria-modal="true"]',
                        '.modal',
                        '.modal__overlay',
                        '.top-level-modal-container',
                        '[class*="privacy" i]',
                        '[id*="privacy" i]',
                        '[class*="cookie" i]',
                        '[id*="cookie" i]',
                        '[class*="consent" i]',
                        '[id*="consent" i]'
                      ].join(','))).filter(isVisible);
                      for (const container of containers) {
                        const text = normalize(container.innerText || container.textContent || '');
                        const isPrivacyNotice = [
                          'privacy notice',
                          'privacy policy',
                          'cookie',
                          'cookies',
                          'manage preferences',
                          'consent',
                          'important notice'
                        ].some((token) => text.includes(token));
                        if (!isPrivacyNotice) continue;
                        const controls = Array.from(container.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"], a'))
                          .filter(isVisible);
                        for (const control of controls) {
                          const label = normalize(control.innerText || control.value || control.getAttribute('aria-label') || control.textContent || '');
                          if (['accept', 'accept all', 'i accept', 'agree', 'i agree', 'ok', 'got it'].includes(label)) {
                            control.click();
                            return label;
                          }
                        }
                      }
                      return '';
                    }
                    """
                )
            except PlaywrightError:
                continue
            label = str(clicked_label or "").strip()
            if label:
                self._record_step(steps, f"Cleared privacy notice by clicking {label}.")
                try:
                    page.wait_for_timeout(700)
                except PlaywrightError:
                    pass
                return True
        return False

    def _should_preserve_existing_value(
        self,
        locator,
        target: dict[str, Any],
        *,
        replacement: str | None = None,
    ) -> bool:
        if target.get("invalid"):
            return False
        if not target.get("has_value"):
            return False
        field_type = (target.get("type") or "").lower()
        if field_type in {"checkbox", "radio"}:
            return True
        value = str(target.get("current_value") or "").strip()
        if not value:
            return False
        placeholder = normalized_text(target.get("placeholder") or "")
        normalized_value = normalized_text(value)
        if placeholder and normalized_value == placeholder:
            return False
        if normalized_value in {"select", "select...", "type here...", "start typing..."}:
            return False
        normalized_replacement = normalized_text(replacement or "")
        if not normalized_replacement or normalized_replacement == normalized_value:
            return True
        label = _field_label(target)
        if _field_value_conflicts_with_label(normalized_value, label):
            return False
        if _looks_like_url_field(label) and _looks_like_url(normalized_replacement) and not _looks_like_url(normalized_value):
            return False
        return True

    def _coerce_field_value(self, value: str, target: dict[str, Any]) -> str:
        field_type = (target.get("type") or "").lower()
        if field_type == "number":
            match = re.search(r"-?\d+(?:\.\d+)?", value)
            return match.group(0) if match else ""
        if field_type == "email":
            match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", value)
            return match.group(0) if match else value.strip()
        return value.strip()

    def _resolve_action_value(
        self,
        value: str | bool | None,
        target: dict[str, Any],
        site_credential: ApplySiteCredential | None,
    ) -> str:
        raw = "" if value is None else str(value)
        if site_credential is None:
            return raw
        if self._is_password_field(target):
            return site_credential.password
        if raw.strip() in {EMAIL_PLACEHOLDER, "{{APPLY_SITE_EMAIL}}"}:
            return site_credential.email
        if raw.strip() in {PASSWORD_PLACEHOLDER, "{{APPLY_SITE_PASSWORD}}"}:
            return site_credential.password
        return raw

    def _match_option(self, frame, value: str):
        pattern = re.compile(rf"^{re.escape(value)}$", re.IGNORECASE)
        option = frame.get_by_role("option", name=pattern)
        if option.count() > 0:
            return option.first
        options = frame.get_by_role("option")
        try:
            texts = options.all_inner_texts()
        except PlaywrightError:
            return None
        target = normalized_text(value)
        for text in texts:
            normalized = normalized_text(text)
            if normalized == target or target in normalized or normalized in target:
                return frame.get_by_role("option", name=re.compile(re.escape(text), re.IGNORECASE)).first
        return None

    def _is_submit_target(self, target: dict[str, Any]) -> bool:
        text = normalized_text(target.get("text") or target.get("label") or "")
        if not text:
            return False
        return any(term == text or term in text for term in SUBMIT_TERMS)

    def _should_require_submit_application_action(
        self,
        target: dict[str, Any],
        targets: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        if not self._is_submit_target(target):
            return False
        return not self._is_application_entry_target(target, targets)

    def _is_application_entry_target(
        self,
        target: dict[str, Any],
        targets: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        text = normalized_text(target.get("text") or target.get("label") or "")
        if not text or any(term in text for term in NON_APPLICATION_ENTRY_TERMS):
            return False

        href = str(target.get("href") or "").strip()
        tag = str(target.get("tag") or "").lower()
        is_entry_text = text in APPLICATION_ENTRY_TERMS or any(term in text for term in APPLICATION_ENTRY_TERMS if term != "apply")
        if not is_entry_text:
            return False

        if tag == "a" and href:
            return True
        if href and "apply" in text:
            return True
        return not self._has_application_form_fields(targets or {})

    def _select_application_entry_target(
        self,
        page: Page,
        detected_ats: str,
        targets: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        candidates = []
        for target in targets.values():
            if target.get("kind") != "button" or target.get("disabled"):
                continue
            if self._is_application_entry_target(target, targets) or self._is_known_job_board_entry_target(
                page, detected_ats, target
            ):
                candidates.append(target)
        if not candidates:
            return None
        candidates.sort(key=self._application_entry_priority)
        return candidates[0]

    def _is_known_job_board_entry_target(self, page: Page, detected_ats: str, target: dict[str, Any]) -> bool:
        text = normalized_text(target.get("text") or target.get("label") or "")
        if not text or any(term in text for term in NON_APPLICATION_ENTRY_TERMS):
            return False
        if not (text in APPLICATION_ENTRY_TERMS or any(term in text for term in APPLICATION_ENTRY_TERMS if term != "apply")):
            return False

        parsed = urlparse(page.url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if detected_ats == "dice" and (host == "dice.com" or host.endswith(".dice.com")):
            return path.startswith("/job-detail/")
        return False

    @staticmethod
    def _application_entry_priority(target: dict[str, Any]) -> tuple[int, int, str]:
        text = normalized_text(target.get("text") or target.get("label") or "")
        if text == "apply now":
            rank = 0
        elif text == "apply":
            rank = 1
        elif "apply" in text:
            rank = 2
        else:
            rank = 3
        return (rank, len(text), text)

    @staticmethod
    def _entry_target_fingerprint(page: Page, target: dict[str, Any]) -> str:
        label = normalized_text(target.get("text") or target.get("label") or "")
        href = normalized_text(target.get("href") or "")
        return "|".join([urlparse(page.url).netloc.lower(), urlparse(page.url).path.lower(), label, href])

    def _has_application_form_fields(self, targets: dict[str, dict[str, Any]]) -> bool:
        for target in targets.values():
            if target.get("kind") != "field":
                continue
            field_type = str(target.get("type") or "").lower()
            if field_type in {"hidden", "search"}:
                continue
            label = normalized_text(" ".join(str(target.get(key) or "") for key in ("label", "placeholder")))
            if not target.get("required") and any(token in label for token in ("search", "keyword", "job title", "location")):
                continue
            return True
        return False

    def _has_observed_form_fields(self, observation: dict[str, Any]) -> bool:
        for frame in observation.get("frames", []):
            for field in frame.get("fields", []):
                field_type = str(field.get("type") or "").lower()
                if field_type in {"hidden", "search"}:
                    continue
                label = normalized_text(" ".join(str(field.get(key) or "") for key in ("label", "placeholder")))
                if not field.get("required") and any(token in label for token in ("search", "keyword", "job title", "location")):
                    continue
                return True
        return False

    def _current_page_looks_like_application_form(self, page: Page) -> bool:
        try:
            observation = self._observe(page, detected_ats="manual_resume_check")
            page_text = normalized_text(" ".join(self._body_text_candidates(page))[:5000])
        except PlaywrightError:
            return False
        return self._observation_looks_like_application_form(observation, page_text=page_text)

    def _observation_looks_like_application_form(self, observation: dict[str, Any], *, page_text: str = "") -> bool:
        normalized_page_text = normalized_text(page_text)
        blockers = set(observation.get("blockers") or [])
        if self._has_auth_blocker(list(blockers)):
            return False

        field_count = 0
        non_auth_field_count = 0
        application_score = 0
        has_resume_upload = False
        for frame in observation.get("frames", []):
            for field in frame.get("fields", []):
                field_type = str(field.get("type") or "").lower()
                if field_type in {"hidden", "search"}:
                    continue
                label = _field_label(field)
                if not field.get("required") and any(token in label for token in ("search", "keyword", "job title", "location")):
                    continue

                field_count += 1
                auth_only = field_type == "password" or any(token in label for token in AUTH_ONLY_FIELD_TERMS)
                application_label = any(token in label for token in APPLICATION_FORM_FIELD_TERMS)
                if not auth_only or application_label:
                    non_auth_field_count += 1
                if field_type == "file" or any(token in label for token in ("resume", "cv", "upload")):
                    has_resume_upload = True
                    application_score += 2
                elif application_label:
                    application_score += 1
                elif field_type in {"checkbox", "radio", "select", "textarea"} and not auth_only:
                    application_score += 1

        if has_resume_upload or application_score >= 2:
            return True

        page_mentions_application = any(token in normalized_page_text for token in APPLICATION_FORM_PAGE_TERMS)
        if field_count >= 4 and non_auth_field_count >= 3 and not _looks_like_auth_gate_text(normalized_page_text):
            return True
        if page_mentions_application and field_count >= 2 and non_auth_field_count >= 1:
            return True

        yes_no_buttons = 0
        next_or_submit_buttons = 0
        for frame in observation.get("frames", []):
            for button in frame.get("buttons", []):
                text = normalized_text(button.get("text") or button.get("label") or "")
                if text in {"yes", "no"}:
                    yes_no_buttons += 1
                elif text in {"next", "continue", "submit", "submit application"}:
                    next_or_submit_buttons += 1
        if page_mentions_application and yes_no_buttons >= 2 and next_or_submit_buttons >= 1:
            return True
        return False

    def _should_yield_dice_profile_detour(self, page: Page, observation: dict[str, Any]) -> bool:
        if observation.get("detected_ats") != "dice_profile":
            return False
        parsed = urlparse(str(observation.get("page_url") or page.url or ""))
        host = parsed.netloc.lower()
        if host != "dice.com" and not host.endswith(".dice.com"):
            return False
        blockers = set(observation.get("blockers") or [])
        if "manual_verification" in blockers or self._has_observed_form_fields(observation):
            return False
        path = parsed.path.lower()
        if path.startswith("/register"):
            return True
        if any(
            "/job-applications/" in str(frame.get("url") or "").lower()
            for frame in observation.get("frames", [])
        ):
            return True
        page_text = normalized_text(self._safe_body_text(page))
        return "where tech connects" in page_text and "create free profile" in page_text

    def _dice_existing_account_error_visible(self, page: Page, observation: dict[str, Any]) -> bool:
        parsed = urlparse(str(observation.get("page_url") or page.url or ""))
        host = parsed.netloc.lower()
        if host != "dice.com" and not host.endswith(".dice.com"):
            return False

        text_parts = [self._safe_body_text(page)]
        for frame in observation.get("frames", []):
            text_parts.extend(str(item or "") for item in frame.get("validation_errors") or [])
        text = normalized_text(" ".join(text_parts))
        return any(
            term in text
            for term in (
                "a user with this email address already exists",
                "user with this email address already exists",
                "email address already exists",
                "email already exists",
                "account already exists",
            )
        ) and any(term in text for term in ("log in", "login", "sign in"))

    def _click_dice_existing_account_login(
        self,
        page: Page,
        observation: dict[str, Any],
        steps: list[str],
    ) -> bool:
        targets = self._target_map(observation)
        candidates: list[dict[str, Any]] = []
        for target in targets.values():
            if target.get("kind") != "button" or target.get("disabled"):
                continue
            text = normalized_text(target.get("text") or target.get("label") or "")
            href = normalized_text(target.get("href") or "")
            combined = " ".join((text, href)).strip()
            if not combined:
                continue
            if not any(term in combined for term in ("click here to log in", "log in", "login", "sign in")):
                continue
            if any(term in combined for term in ("register now", "register", "create free profile", "create profile")):
                continue
            candidates.append(target)

        if not candidates:
            return False

        candidates.sort(key=_dice_existing_account_login_priority)
        target = candidates[0]
        action = ApplyAction(
            action="click",
            element_id=str(target.get("id") or ""),
            reasoning="Dice reported an existing account, so switch from registration to login.",
            confidence=0.96,
        )
        try:
            result = self._execute_action(
                page=page,
                action=action,
                targets=targets,
                resume_path=Path(),
                screenshot_path=Path(),
                auto_submit=False,
            )
            self._total_actions += 1
            self._log_action(action, result["status"], result["message"], result.get("target"))
            if result["status"] != "success":
                return False
            self._record_step(steps, "Dice reported an existing account; opened the Dice login path.")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1200)
            return True
        except PlaywrightError:
            return False

    def _selector_for_observed_target(self, item: dict[str, Any]) -> str:
        target_id = str(item.get("id") or "")
        raw_id = target_id.split(":", 1)[1] if ":" in target_id else target_id
        if not raw_id:
            return ""
        if item.get("kind") == "field":
            return f'[data-apply-agent-id="{raw_id}"]'
        if item.get("kind") == "button":
            return f'[data-apply-agent-button-id="{raw_id}"]'
        return ""

    def _is_obviously_unrelated_navigation(self, page: Page, target: dict[str, Any]) -> bool:
        href = str(target.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            return False
        label = normalized_text(target.get("text") or "")
        if any(token in label for token in ("linkedin", "facebook", "twitter", "instagram", "youtube", "blog", "press")):
            return True
        current = urlparse(page.url)
        destination = urlparse(urljoin(page.url, href))
        if not destination.netloc:
            return False
        current_host = current.netloc.lower()
        destination_host = destination.netloc.lower()
        if destination_host == current_host:
            return False
        if _is_known_application_host(destination_host):
            return False
        path = normalized_text(destination.path.replace("/", " "))
        if _is_auth_button_text(label):
            return False
        if any(token in path for token in ("login", "signin", "sign in", "account", "payment", "billing")):
            return True
        if any(token in label for token in ("apply", "continue", "next", "review", "submit")):
            return False
        return False

    def _is_repeated_action(self, action: ApplyAction) -> bool:
        fingerprint = "|".join(
            [
                normalized_text(action.action),
                str(action.element_id or ""),
                str(action.value or "")[:80],
            ]
        )
        count = self._action_fingerprints.get(fingerprint, 0) + 1
        self._action_fingerprints[fingerprint] = count
        return count > MAX_REPEATED_ACTIONS

    def _log_action(
        self,
        action: ApplyAction,
        status: str,
        message: str,
        target: dict[str, Any] | None = None,
    ) -> None:
        target_label = str((target or {}).get("label") or (target or {}).get("text") or "")[:180] if target else None
        entry = {
            "action": action.action,
            "element_id": action.element_id,
            "status": status,
            "message": message,
            "target_kind": target.get("kind") if target else None,
            "target_label": target_label,
            "value_category": _value_category(action, target),
            "reasoning": action.reasoning,
            "confidence": action.confidence,
        }
        self._action_log.append(entry)
        if len(self._action_log) > ACTION_LOG_LIMIT:
            self._action_log = self._action_log[-ACTION_LOG_LIMIT:]

    def _record_step(self, steps: list[str], message: str) -> None:
        steps.append(message)
        logger.info("AI browser apply loop step: %s", message)

    def _result(
        self,
        *,
        job: Job,
        detected_ats: str,
        status: str,
        success: bool,
        screenshot_path: Path,
        confirmation_text: str | None,
        steps: list[str],
        error: str | None = None,
    ) -> ApplyJobResult:
        self._remove_current_activity_overlay()
        action_log = self._trimmed_action_log()
        if success and not self._retain_success_logs:
            action_log = []
        return ApplyJobResult(
            job_id=job.id,
            company_name=job.company_name,
            job_title=job.job_title,
            ats_type=detected_ats or self.ats_type,
            status=status,
            success=success,
            error=error,
            screenshot_path=str(screenshot_path) if screenshot_path.exists() else None,
            confirmation_text=confirmation_text,
            steps=steps,
            action_log=action_log,
        )

    def _ambiguous_result(
        self,
        *,
        page: Page,
        job: Job,
        detected_ats: str,
        screenshot_path: Path,
        steps: list[str],
        message: str,
    ) -> ApplyJobResult:
        self._remove_activity_overlay(page)
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        if self._submit_attempts > 0:
            self._record_step(steps, f"{message} Submission state needs user confirmation.")
            return self._result(
                job=job,
                detected_ats=detected_ats,
                status="manual_closed",
                success=False,
                screenshot_path=screenshot_path,
                confirmation_text=message,
                steps=steps,
            )
        return self._result(
            job=job,
            detected_ats=detected_ats,
            status="needs_review",
            success=False,
            screenshot_path=screenshot_path,
            confirmation_text=message,
            steps=steps,
        )

    def _await_manual_resolution(
        self,
        *,
        page: Page,
        job: Job,
        detected_ats: str,
        screenshot_path: Path,
        steps: list[str],
        cancel_checker,
        message: str,
    ) -> ApplyJobResult | None:
        started_at = time.monotonic()
        self._remove_activity_overlay(page)
        self._show_manual_completion_overlay(page, message=message)
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        while True:
            if page.is_closed():
                break
            self._check_cancelled(cancel_checker)
            confirmation_text = self._success_text(page)
            if confirmation_text:
                try:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception:
                    pass
                return self._result(
                    job=job,
                    detected_ats=detected_ats,
                    status="submitted",
                    success=True,
                    screenshot_path=screenshot_path,
                    confirmation_text=confirmation_text,
                    steps=steps,
                )
            choice = self._manual_overlay_choice(page)
            if choice == "resume":
                self._remove_manual_overlay(page)
                self._record_step(steps, "User asked AI to resume after manual login or verification.")
                self._return_to_manual_resume_url(page, steps)
                return None
            if choice == "manual":
                return self._await_user_manual_finish(
                    page=page,
                    job=job,
                    detected_ats=detected_ats,
                    screenshot_path=screenshot_path,
                    steps=steps,
                    cancel_checker=cancel_checker,
                    message=message,
                )
            if choice == "cancel":
                self._record_step(steps, "User cancelled manual application completion.")
                break
            if time.monotonic() - started_at > MANUAL_RESUME_TIMEOUT_SECONDS:
                self._record_step(steps, "Manual intervention timed out.")
                break
            try:
                page.wait_for_timeout(1000)
            except PlaywrightError:
                if page.is_closed():
                    break
                raise
        return self._result(
            job=job,
            detected_ats=detected_ats,
            status="manual_closed",
            success=False,
            screenshot_path=screenshot_path,
            confirmation_text=message or MANUAL_COMPLETION_MESSAGE,
            steps=steps,
        )

    def _return_to_manual_resume_url(self, page: Page, steps: list[str]) -> None:
        resume_url = (self._manual_resume_url or "").strip()
        if not resume_url or page.is_closed():
            return
        if self._current_page_looks_like_application_form(page):
            self._record_step(
                steps,
                "Resume AI found the current page already contains an application form; continuing without navigation.",
            )
            return
        if _urls_match_without_fragment(page.url, resume_url):
            return
        try:
            page.goto(resume_url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(1200)
            self._record_step(steps, f"Returned to saved application URL after manual resume: {resume_url}")
        except PlaywrightError as exc:
            self._record_step(steps, f"Saved application URL did not load after manual resume: {exc}")

    def _await_user_manual_finish(
        self,
        *,
        page: Page,
        job: Job,
        detected_ats: str,
        screenshot_path: Path,
        steps: list[str],
        cancel_checker,
        message: str,
    ) -> ApplyJobResult:
        self._remove_manual_overlay(page)
        self._record_step(steps, "User chose to finish the application manually; keeping the browser open.")
        started_at = time.monotonic()
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass

        while True:
            if page.is_closed():
                self._record_step(steps, "Manual application window closed by user.")
                break
            self._check_cancelled(cancel_checker)
            confirmation_text = self._success_text(page)
            if confirmation_text:
                try:
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception:
                    pass
                self._record_step(steps, f"Detected successful submission after manual completion: {confirmation_text}")
                return self._result(
                    job=job,
                    detected_ats=detected_ats,
                    status="submitted",
                    success=True,
                    screenshot_path=screenshot_path,
                    confirmation_text=confirmation_text,
                    steps=steps,
                )
            if time.monotonic() - started_at > MANUAL_RESUME_TIMEOUT_SECONDS:
                self._record_step(steps, "Manual application window remained open until the timeout.")
                break
            try:
                page.wait_for_timeout(1000)
            except PlaywrightError:
                if page.is_closed():
                    self._record_step(steps, "Manual application window closed by user.")
                    break
                raise

        return self._result(
            job=job,
            detected_ats=detected_ats,
            status="manual_closed",
            success=False,
            screenshot_path=screenshot_path,
            confirmation_text=message or MANUAL_COMPLETION_MESSAGE,
            steps=steps,
        )

    def _show_activity_overlay(self, page: Page) -> None:
        try:
            page.evaluate(
                """
                () => {
                  if (!document.body) return;
                  if (document.getElementById('__apply-agent-manual-modal__') || document.getElementById('__apply-agent-manual-dock__')) {
                    document.getElementById('__apply-agent-activity-overlay__')?.remove();
                    return;
                  }

                  if (!document.getElementById('__apply-agent-activity-style__')) {
                    const style = document.createElement('style');
                    style.id = '__apply-agent-activity-style__';
                    style.textContent = `
                      @keyframes __applyAgentWaveBar {
                        0%, 100% { opacity: .38; transform: scaleY(.34); }
                        45% { opacity: 1; transform: scaleY(1); }
                      }
                      @keyframes __applyAgentBreathe {
                        0%, 100% { opacity: .88; transform: translateY(0) scale(1); }
                        50% { opacity: 1; transform: translateY(-2px) scale(1.015); }
                      }
                      #__apply-agent-activity-wave__ i {
                        width: 7px;
                        height: 46px;
                        border-radius: 999px;
                        background: linear-gradient(180deg, rgba(248,250,252,.95), rgba(147,197,253,.76));
                        box-shadow: inset 0 1px 0 rgba(255,255,255,.42);
                        display: block;
                        transform-origin: 50% 50%;
                        animation: __applyAgentWaveBar 1.18s cubic-bezier(.44,0,.22,1) infinite;
                        animation-delay: calc(var(--wave-index) * -82ms);
                      }
                      @media (prefers-reduced-motion: reduce) {
                        #__apply-agent-activity-wave__ i {
                          animation: none !important;
                        }
                        #__apply-agent-activity-card__ {
                          animation: none !important;
                        }
                      }
                    `;
                    (document.head || document.documentElement).appendChild(style);
                  }

                  if (document.getElementById('__apply-agent-activity-overlay__')) return;

                  const overlay = document.createElement('div');
                  overlay.id = '__apply-agent-activity-overlay__';
                  overlay.setAttribute('aria-hidden', 'true');
                  overlay.style.cssText = [
                    'position:fixed',
                    'inset:0',
                    'z-index:2147483646',
                    'display:grid',
                    'place-items:center',
                    'pointer-events:none',
                    'background:rgba(248,250,252,.10)',
                    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif'
                  ].join(';');
                  overlay.innerHTML = `
                    <div id="__apply-agent-activity-card__" style="display:flex;flex-direction:column;align-items:center;gap:14px;min-width:230px;padding:28px 30px;border-radius:30px;background:rgba(21,41,107,.88);color:#f8fafc;border:1px solid rgba(255,255,255,.28);box-shadow:0 26px 80px rgba(15,23,42,.32), inset 0 1px 0 rgba(255,255,255,.18);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);animation:__applyAgentBreathe 2.6s ease-in-out infinite">
                      <div id="__apply-agent-activity-wave__" aria-hidden="true" style="position:relative;width:132px;height:72px;display:flex;align-items:center;justify-content:center;gap:6px;overflow:hidden;border-radius:26px;background:transparent;box-shadow:none">
                        <i style="--wave-index:0"></i>
                        <i style="--wave-index:1"></i>
                        <i style="--wave-index:2"></i>
                        <i style="--wave-index:3"></i>
                        <i style="--wave-index:4"></i>
                        <i style="--wave-index:5"></i>
                        <i style="--wave-index:6"></i>
                        <i style="--wave-index:7"></i>
                        <i style="--wave-index:8"></i>
                      </div>
                      <span style="font-size:18px;font-weight:850;letter-spacing:-.02em">AI Job Agents is filling this out</span>
                      <span style="max-width:260px;text-align:center;font-size:13px;line-height:1.35;color:rgba(248,250,252,.82)">Some steps can take a moment while the page loads or the LLM decides what to do next.</span>
                    </div>
                  `;
                  document.body.appendChild(overlay);
                }
                """
            )
        except PlaywrightError:
            pass

    def _remove_activity_overlay(self, page: Page) -> None:
        try:
            page.evaluate(
                """
                () => {
                  document.getElementById('__apply-agent-activity-overlay__')?.remove();
                  document.getElementById('__apply-agent-activity-style__')?.remove();
                }
                """
            )
        except PlaywrightError:
            pass

    def _remove_current_activity_overlay(self) -> None:
        try:
            self._remove_activity_overlay(self._current_page)
        except Exception:
            pass

    def _show_manual_completion_overlay(self, page: Page, *, message: str) -> None:
        try:
            page.evaluate(
                """
                ({ message }) => {
                  document.getElementById('__apply-agent-activity-overlay__')?.remove();
                  document.getElementById('__apply-agent-activity-style__')?.remove();
                  const existing = document.getElementById('__apply-agent-manual-modal__');
                  if (existing) existing.remove();
                  const existingDock = document.getElementById('__apply-agent-manual-dock__');
                  if (existingDock) existingDock.remove();
                  window.__applyAgentManualChoice = null;
                  const escapedMessage = String(message || '').replace(/[&<>"']/g, (char) => ({
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;',
                  }[char]));
                  const overlay = document.createElement('div');
                  overlay.id = '__apply-agent-manual-modal__';
                  overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:24px;';
                  overlay.innerHTML = `
                    <div style="max-width:620px;background:#f7f3ec;color:#191919;border-radius:24px;padding:28px 30px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;box-shadow:0 24px 80px rgba(0,0,0,.22)">
                      <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px;color:#1d4ed8">AI Job Agents</div>
                      <h2 style="margin:0 0 12px;font-size:34px;line-height:1.05">Manual step needed</h2>
                      <p style="font-size:18px;line-height:1.45;margin:0 0 20px">${escapedMessage}</p>
                      <div style="display:flex;gap:10px;flex-wrap:wrap">
                        <button id="__apply-agent-user-handle__" type="button" style="border:0;border-radius:999px;background:#1d4ed8;color:white;padding:13px 18px;font-weight:700;cursor:pointer">I'll handle this step</button>
                        <button id="__apply-agent-resume__" type="button" style="border:1px solid #1d4ed8;border-radius:999px;background:white;color:#1d4ed8;padding:12px 17px;font-weight:700;cursor:pointer">Resume AI</button>
                        <button id="__apply-agent-manual__" type="button" style="border:1px solid #a8a29e;border-radius:999px;background:white;color:#191919;padding:12px 17px;font-weight:700;cursor:pointer">I'll finish manually</button>
                        <button id="__apply-agent-cancel__" type="button" style="border:0;border-radius:999px;background:#991b1b;color:white;padding:13px 18px;font-weight:700;cursor:pointer">Cancel</button>
                      </div>
                    </div>
                  `;
                  const showDock = () => {
                    overlay.remove();
                    const dock = document.createElement('div');
                    dock.id = '__apply-agent-manual-dock__';
                    dock.style.cssText = 'position:fixed;left:16px;right:16px;top:16px;z-index:2147483647;background:#1e3a8a;color:#f8fafc;border:1px solid rgba(245,243,238,.22);border-radius:18px;padding:12px 14px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;box-shadow:0 20px 48px rgba(21,41,107,.28);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap';
                    dock.innerHTML = `
                      <span style="font-size:14px;line-height:1.35;font-weight:650;letter-spacing:-.01em">AI is waiting while you complete login, registration, verification, or CAPTCHA.</span>
                      <span style="display:flex;gap:8px;flex-wrap:wrap">
                        <button id="__apply-agent-dock-resume__" type="button" style="border:0;border-radius:999px;background:#f5f3ee;color:#15296b;padding:9px 14px;font-weight:850;cursor:pointer;box-shadow:inset 0 0 0 1px rgba(255,255,255,.28)">Resume AI</button>
                        <button id="__apply-agent-dock-manual__" type="button" style="border:1px solid rgba(245,243,238,.5);border-radius:999px;background:rgba(245,243,238,.08);color:#f8fafc;padding:8px 13px;font-weight:750;cursor:pointer">I'll finish manually</button>
                        <button id="__apply-agent-dock-cancel__" type="button" style="border:0;border-radius:999px;background:#8b3030;color:white;padding:9px 13px;font-weight:850;cursor:pointer">Cancel</button>
                      </span>
                    `;
                    dock.querySelector('#__apply-agent-dock-resume__')?.addEventListener('click', () => { window.__applyAgentManualChoice = 'resume'; dock.remove(); });
                    dock.querySelector('#__apply-agent-dock-manual__')?.addEventListener('click', () => { window.__applyAgentManualChoice = 'manual'; dock.remove(); });
                    dock.querySelector('#__apply-agent-dock-cancel__')?.addEventListener('click', () => { window.__applyAgentManualChoice = 'cancel'; dock.remove(); });
                    document.body.appendChild(dock);
                  };
                  overlay.querySelector('#__apply-agent-user-handle__')?.addEventListener('click', showDock);
                  overlay.querySelector('#__apply-agent-resume__')?.addEventListener('click', () => { window.__applyAgentManualChoice = 'resume'; overlay.remove(); });
                  overlay.querySelector('#__apply-agent-manual__')?.addEventListener('click', () => { window.__applyAgentManualChoice = 'manual'; overlay.remove(); });
                  overlay.querySelector('#__apply-agent-cancel__')?.addEventListener('click', () => { window.__applyAgentManualChoice = 'cancel'; overlay.remove(); });
                  document.body.appendChild(overlay);
                }
                """,
                {"message": message or MANUAL_COMPLETION_MESSAGE},
            )
        except PlaywrightError:
            pass

    def _manual_overlay_choice(self, page: Page) -> str:
        try:
            return str(page.evaluate("() => window.__applyAgentManualChoice || ''") or "")
        except PlaywrightError:
            return ""

    def _remove_manual_overlay(self, page: Page) -> None:
        try:
            page.evaluate(
                """
                () => {
                  document.getElementById('__apply-agent-manual-modal__')?.remove();
                  document.getElementById('__apply-agent-manual-dock__')?.remove();
                  window.__applyAgentManualChoice = null;
                }
                """
            )
        except PlaywrightError:
            pass

    def _trimmed_action_log(self) -> list[dict[str, Any]]:
        return self._action_log[-ACTION_LOG_LIMIT:]

    def _check_cancelled(self, cancel_checker) -> None:
        if cancel_checker and cancel_checker():
            raise RuntimeError("Apply agent cancelled.")


class ManualHandoffRequested(RuntimeError):
    pass


class NeedsReviewRequested(RuntimeError):
    pass


class AmbiguousSubmitState(RuntimeError):
    pass


def _public_target(target: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in target.items()
        if key not in {"frame", "selector"}
    }


def _value_category(action: ApplyAction, target: dict[str, Any] | None) -> str | None:
    if action.action in {"wait", "scroll", "screenshot", "click", "submit_application", "upload_resume"}:
        return None
    label = normalized_text((target or {}).get("label") or (target or {}).get("text") or "")
    if "email" in label:
        return "email"
    if "phone" in label:
        return "phone"
    if "name" in label:
        return "name"
    if "sponsor" in label or "authorized" in label or "work authorization" in label:
        return "work_authorization"
    if "gender" in label or "veteran" in label or "disability" in label or "hispanic" in label:
        return "eeo"
    if "salary" in label or "compensation" in label:
        return "compensation"
    return "application_answer"


def _coerce_checkbox_value(value: str | bool | None, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = normalized_text(str(value or ""))
    if normalized in {"false", "no", "n", "0", "off", "unchecked", "uncheck"}:
        return False
    if normalized in {"true", "yes", "y", "1", "on", "checked", "check"}:
        return True
    return default


def _field_label(target: dict[str, Any]) -> str:
    return normalized_text(
        " ".join(
            str(target.get(key) or "")
            for key in ("label", "placeholder", "name", "id")
        )
    )


def _split_name(name: str) -> tuple[str, str]:
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _looks_like_first_name_field(label: str) -> bool:
    return any(token in label for token in ("first name", "given name")) and "last" not in label


def _looks_like_last_name_field(label: str) -> bool:
    return any(token in label for token in ("last name", "family name", "surname"))


def _looks_like_full_name_field(label: str) -> bool:
    if "first name" in label or "last name" in label:
        return False
    return label in {"name", "full name", "legal name"} or "full legal name" in label


def _looks_like_url(value: str) -> bool:
    normalized = normalized_text(value)
    return normalized.startswith("http://") or normalized.startswith("https://")


def _looks_like_url_field(label: str) -> bool:
    return any(token in label for token in ("link", "url", "linkedin", "website", "portfolio", "github"))


def _field_value_conflicts_with_label(value: str, label: str) -> bool:
    if not value or not label:
        return False
    has_email = bool(re.search(r"[\w.+-]+@[\w.-]+\.\w+", value))
    has_phone = bool(re.search(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}", value))
    has_url = _looks_like_url(value)
    label_is_email = "email" in label
    label_is_phone = "phone" in label or "mobile" in label
    label_is_name = _looks_like_first_name_field(label) or _looks_like_last_name_field(label) or _looks_like_full_name_field(label)
    if has_email and not label_is_email:
        return True
    if has_phone and not label_is_phone:
        return True
    if has_url and not _looks_like_url_field(label):
        return True
    if _looks_like_url_field(label) and value and not has_url:
        return True
    if label_is_name and (has_email or has_phone or has_url):
        return True
    return False


def _is_sso_button_text(text: str) -> bool:
    return any(
        token in text
        for token in (
            "google",
            "apple",
            "facebook",
            "linkedin",
            "github",
            "sso",
            "single sign",
            "saml",
        )
    )


def _is_auth_button_text(text: str) -> bool:
    return any(
        token in text
        for token in (
            "continue with email",
            "continue",
            "next",
            "sign in",
            "signin",
            "log in",
            "login",
            "join with email",
            "join",
            "create account",
            "create an account",
            "register",
            "sign up",
            "signup",
            "start application",
        )
    )


def _auth_targets_prefer_login(targets: dict[str, dict[str, Any]]) -> bool:
    password_fields = []
    has_confirm_password = False
    has_login_button = False
    for target in targets.values():
        if target.get("kind") == "field":
            field_type = str(target.get("type") or "").lower()
            label = _field_label(target)
            if field_type == "password" or "password" in label:
                password_fields.append(target)
                if "confirm" in label or "re-enter" in label or "reenter" in label:
                    has_confirm_password = True
        elif target.get("kind") == "button":
            text = normalized_text(target.get("text") or target.get("label") or "")
            if any(term in text for term in ("sign in", "signin", "log in", "login")):
                has_login_button = True
    return bool(password_fields) and not has_confirm_password and has_login_button


def _submit_button_priority(target: dict[str, Any]) -> tuple[int, int, str]:
    text = normalized_text(target.get("text") or target.get("label") or "")
    if text == "submit":
        rank = 0
    elif "submit application" in text:
        rank = 1
    elif "submit" in text:
        rank = 2
    else:
        rank = 3
    return (rank, len(text), text)


def _auth_button_priority(target: dict[str, Any], *, prefer_login: bool = False) -> tuple[int, int, str]:
    text = normalized_text(target.get("text") or target.get("label") or "")
    if prefer_login and ("sign in" in text or "signin" in text or "log in" in text or "login" in text):
        rank = 0
    elif "continue with email" in text or "join with email" in text:
        rank = 1 if prefer_login else 0
    elif "create account" in text or "create an account" in text or "register" in text or "sign up" in text:
        rank = 4 if prefer_login else 1
    elif "continue" in text or text == "next":
        rank = 2
    elif "sign in" in text or "log in" in text or "login" in text:
        rank = 0 if prefer_login else 3
    else:
        rank = 4
    return (rank, len(text), text)


def _dice_existing_account_login_priority(target: dict[str, Any]) -> tuple[int, int, str]:
    text = normalized_text(target.get("text") or target.get("label") or "")
    href = normalized_text(target.get("href") or "")
    combined = " ".join((text, href)).strip()
    if "click here to log in" in combined:
        rank = 0
    elif "/login" in href or "login" in href:
        rank = 1
    elif text in {"log in", "login", "sign in", "signin"}:
        rank = 2
    elif "already have an account" in combined:
        rank = 3
    else:
        rank = 4
    return (rank, len(combined), combined)


def _first_confirmation_line(body_text: str) -> str:
    for line in body_text.splitlines():
        cleaned = line.strip()
        if cleaned and len(cleaned) > 6:
            return cleaned
    return "Application submitted."


def _urls_match_without_fragment(left: str | None, right: str | None) -> bool:
    left_parsed = urlparse(left or "")
    right_parsed = urlparse(right or "")
    return left_parsed._replace(fragment="").geturl() == right_parsed._replace(fragment="").geturl()


def _looks_like_auth_gate_text(text: str) -> bool:
    normalized = normalized_text(text)
    if not normalized:
        return False
    auth_phrases = (
        "agree & join linkedin",
        "agree and join linkedin",
        "join with email",
        "already on linkedin sign in",
        "sign in with google",
        "use your google account to sign in",
        "continue with google",
        "continue with apple",
        "sign in to apply",
        "log in to apply",
        "login to apply",
        "create an account to apply",
        "create account to apply",
        "create your account",
        "create a candidate home account",
        "candidate home account",
        "already have an account",
        "already registered",
        "new user registration",
        "register to apply",
        "sign up to apply",
        "enter your email address to continue",
        "enter your email to continue",
        "password is required",
        "forgot password",
        "reset password",
    )
    return any(phrase in normalized for phrase in auth_phrases)


def _looks_like_active_manual_verification(text: str, *, iframe_sources: str = "") -> bool:
    normalized = normalized_text(text)
    sources = normalized_text(iframe_sources)
    passive_terms = (
        "protected by recaptcha",
        "google privacy policy and terms of service apply",
        "privacy policy terms of service apply",
    )
    if normalized and any(term in normalized for term in passive_terms):
        normalized = normalized
        for term in passive_terms:
            normalized = normalized.replace(term, "")
    active_terms = (
        "i'm not a robot",
        "im not a robot",
        "verify you are human",
        "verify that you are human",
        "complete the captcha",
        "complete captcha",
        "captcha challenge",
        "security verification",
        "hcaptcha",
        "cf-turnstile",
        "turnstile challenge",
    )
    active_source_terms = (
        "hcaptcha.com",
        "challenges.cloudflare.com",
        "api2/anchor",
        "api2/bframe",
        "recaptcha/enterprise/anchor",
        "recaptcha/enterprise/bframe",
    )
    return any(term in normalized for term in active_terms) or any(term in sources for term in active_source_terms)


def _is_known_application_host(hostname: str) -> bool:
    return any(
        token in hostname
        for token in (
            "greenhouse.io",
            "ashbyhq.com",
            "icims.com",
            "lever.co",
            "workdayjobs.com",
            "smartrecruiters.com",
            "workable.com",
            "jobvite.com",
            "bamboohr.com",
            "dice.com",
            "appcast.io",
            "oraclecloud.com",
            "workdayjobs.com",
            "myworkdayjobs.com",
            "myworkdaysite.com",
            "linkedin.com",
        )
    )
