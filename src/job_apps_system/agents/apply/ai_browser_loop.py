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


logger = logging.getLogger(__name__)

AI_BROWSER_ATS_TYPE = "ai_browser"
MAX_AI_TURNS = 20
MAX_ACTIONS_PER_TURN = 5
MAX_TOTAL_ACTIONS = 60
MAX_SUBMIT_ATTEMPTS = 4
MAX_RUNTIME_SECONDS = 8 * 60
MAX_REPEATED_ACTIONS = 2
ACTION_LOG_LIMIT = 80

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
)

SUBMIT_TERMS = (
    "submit",
    "submit application",
    "send application",
    "apply",
    "send",
)

HARD_STOP_TERMS = (
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

MANUAL_COMPLETION_MESSAGE = (
    "AI Job Agents filled the application as far as it safely could. Complete the remaining step in this browser window."
)


class AiBrowserApplyLoop:
    ats_type = AI_BROWSER_ATS_TYPE

    def __init__(self, *, retain_success_logs: bool = False) -> None:
        self._retain_success_logs = retain_success_logs
        self._action_log: list[dict[str, Any]] = []
        self._submit_attempts = 0
        self._total_actions = 0
        self._action_fingerprints: dict[str, int] = {}

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
        initial_reason: str = "",
        cancel_checker=None,
    ) -> ApplyJobResult:
        steps: list[str] = []
        started_at = time.monotonic()
        self._current_page = page
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

                observation = self._observe(page, detected_ats=detected_ats)
                blockers = observation.get("blockers") or []
                if self._has_hard_stop(blockers):
                    self._record_step(steps, "AI loop detected a manual-only blocker.")
                    return self._await_manual_completion(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                    )
                if self._submit_attempts > 0 and self._has_manual_submit_blocker(observation):
                    self._record_step(steps, "AI loop detected a concrete post-submit blocker.")
                    return self._await_manual_completion(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                    )

                plan = answer_service.generate_browser_action_plan(
                    observation=observation,
                    applicant=applicant,
                    job=job,
                    auto_submit=auto_submit,
                    recent_actions=self._action_log,
                )
                self._record_step(steps, f"AI browser loop turn {turn}: {plan.summary or 'received action plan'}.")

                if plan.needs_manual:
                    self._record_step(steps, plan.summary or "AI loop requested manual completion.")
                    return self._await_manual_completion(
                        page=page,
                        job=job,
                        detected_ats=detected_ats,
                        screenshot_path=screenshot_path,
                        steps=steps,
                        cancel_checker=cancel_checker,
                    )
                if plan.needs_review or plan.terminal or plan.done:
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
                        )
                    except ManualHandoffRequested as exc:
                        self._record_step(steps, str(exc))
                        return self._await_manual_completion(
                            page=page,
                            job=job,
                            detected_ats=detected_ats,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            cancel_checker=cancel_checker,
                        )
                    except NeedsReviewRequested as exc:
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

    def _execute_action(
        self,
        *,
        page: Page,
        action: ApplyAction,
        targets: dict[str, dict[str, Any]],
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
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
            if self._should_preserve_existing_value(locator, target):
                return {"status": "skipped", "message": "preserved_existing_value", "target": target}
            value = self._coerce_field_value(str(action.value or ""), target)
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
            if self._is_submit_target(target):
                return {"status": "skipped", "message": "submit_requires_submit_application_action", "target": target}
            if self._is_obviously_unrelated_navigation(page, target):
                return {"status": "skipped", "message": "unrelated_navigation_blocked", "target": target}
            locator.click(timeout=10000)
            page.wait_for_timeout(1200)
            return {"status": "success", "message": "clicked", "target": target}

        if action_type == "submit_application":
            if not auto_submit:
                raise NeedsReviewRequested("Auto-submit is disabled.")
            if kind != "button" or not self._is_submit_target(target):
                return {"status": "skipped", "message": "submit_target_not_final_submit", "target": target}
            if str(target.get("tag") or "").lower() == "a" and str(target.get("href") or "").strip():
                return {"status": "skipped", "message": "submit_target_is_navigation", "target": target}
            errors = self._visible_validation_errors(target["frame"])
            if errors:
                return {"status": "skipped", "message": "validation_errors_present", "target": target}
            self._submit_attempts += 1
            if self._submit_attempts > MAX_SUBMIT_ATTEMPTS:
                raise AmbiguousSubmitState("Submit attempt limit reached.")
            locator.click(timeout=10000)
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
            if expected:
                locator.check(timeout=8000, force=True)
            else:
                locator.uncheck(timeout=8000, force=True)
            return {"status": "success", "message": "selected_checkbox", "target": target}
        if field_type == "radio":
            locator.check(timeout=8000, force=True)
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
        try:
            if (field.type or "").lower() in {"checkbox", "radio"}:
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
        return {
            "has_value": bool(str(value).strip()) or checked is True,
            "current_value": str(value)[:120],
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
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled;
                  })
                  .slice(0, 80)
                  .map((el, index) => {
                    const id = `btn_${String(index + 1).padStart(3, '0')}`;
                    el.setAttribute('data-apply-agent-button-id', id);
                    return {
                      element_id: id,
                      text: ((el.innerText || el.value || el.getAttribute('aria-label') || el.textContent || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 180),
                      tag: el.tagName.toLowerCase(),
                      href: el.getAttribute('href') || '',
                      selector: `[data-apply-agent-button-id="${id}"]`,
                      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
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
                    "tag": row.get("tag") or "",
                    "href": row.get("href") or "",
                    "disabled": bool(row.get("disabled")),
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
                () => {
                  const text = (document.body?.innerText || '').toLowerCase();
                  const iframeSources = Array.from(document.querySelectorAll('iframe')).map((item) => item.src || '');
                  const visiblePassword = Array.from(document.querySelectorAll('input[type="password"]'))
                    .some((el) => {
                      const style = window.getComputedStyle(el);
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled;
                    });
                  return { text, iframeSources, visiblePassword };
                }
                """
            )
        except PlaywrightError:
            return []
        text = normalized_text(str(payload.get("text") or ""))
        iframe_sources = " ".join(str(item).lower() for item in payload.get("iframeSources") or [])
        blockers: list[str] = []
        if any(token in text or token in iframe_sources for token in ("captcha", "recaptcha", "hcaptcha", "turnstile")):
            blockers.append("manual_verification")
        if payload.get("visiblePassword"):
            blockers.append("password_field")
        for token in HARD_STOP_TERMS:
            if token in text:
                blockers.append(token)
        return sorted(set(blockers))

    def _page_blockers(self, frames: list[dict[str, Any]], page_text: str) -> list[str]:
        blockers: set[str] = set()
        for frame in frames:
            blockers.update(frame.get("blockers") or [])
        normalized = normalized_text(page_text)
        for token in HARD_STOP_TERMS:
            if token in normalized:
                blockers.add(token)
        return sorted(blockers)

    def _success_text(self, page: Page) -> str:
        for text in self._body_text_candidates(page):
            normalized = normalized_text(text)
            if any(term in normalized for term in SUCCESS_TERMS):
                return _first_confirmation_line(text)
        return ""

    def _body_text_candidates(self, page: Page) -> list[str]:
        texts: list[str] = []
        try:
            texts.append(page.locator("body").inner_text(timeout=1000))
        except PlaywrightError:
            pass
        for frame in page.frames:
            try:
                texts.append(frame.locator("body").inner_text(timeout=1000))
            except PlaywrightError:
                continue
        return [text for text in texts if isinstance(text, str) and text.strip()]

    def _safe_body_text(self, page: Page) -> str:
        texts = self._body_text_candidates(page)
        compact = " ".join(" ".join(text.split()) for text in texts)
        return compact[:1200]

    def _has_hard_stop(self, blockers: list[str]) -> bool:
        return any(blocker in HARD_STOP_TERMS for blocker in blockers)

    def _has_manual_submit_blocker(self, observation: dict[str, Any]) -> bool:
        blockers = set(observation.get("blockers") or [])
        if "manual_verification" in blockers:
            return True
        for frame in observation.get("frames", []):
            errors = frame.get("validation_errors") or []
            if errors:
                return False
            for button in frame.get("buttons", []):
                if self._is_submit_target(button) and button.get("disabled"):
                    return True
        return False

    def _should_preserve_existing_value(self, locator, target: dict[str, Any]) -> bool:
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
        entry = {
            "action": action.action,
            "element_id": action.element_id,
            "status": status,
            "message": message,
            "target_kind": target.get("kind") if target else None,
            "target_label": (target.get("label") or target.get("text"))[:180] if target else None,
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

    def _await_manual_completion(
        self,
        *,
        page: Page,
        job: Job,
        detected_ats: str,
        screenshot_path: Path,
        steps: list[str],
        cancel_checker,
    ) -> ApplyJobResult:
        self._show_manual_completion_overlay(page)
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
            confirmation_text=MANUAL_COMPLETION_MESSAGE,
            steps=steps,
        )

    def _show_manual_completion_overlay(self, page: Page) -> None:
        try:
            page.evaluate(
                """
                ({ message }) => {
                  const existing = document.getElementById('__apply-agent-manual-modal__');
                  if (existing) existing.remove();
                  const overlay = document.createElement('div');
                  overlay.id = '__apply-agent-manual-modal__';
                  overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:24px;';
                  overlay.innerHTML = `
                    <div style="max-width:620px;background:#f7f3ec;color:#191919;border-radius:24px;padding:28px 30px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;box-shadow:0 24px 80px rgba(0,0,0,.22)">
                      <div style="font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin-bottom:10px;color:#1d4ed8">AI Job Agents</div>
                      <h2 style="margin:0 0 12px;font-size:34px;line-height:1.05">Manual completion needed</h2>
                      <p style="font-size:18px;line-height:1.45;margin:0 0 20px">${message}</p>
                      <button id="__apply-agent-manual-dismiss__" type="button" style="border:0;border-radius:999px;background:#1d4ed8;color:white;padding:13px 18px;font-weight:700;cursor:pointer">Continue manually</button>
                    </div>
                  `;
                  overlay.querySelector('#__apply-agent-manual-dismiss__')?.addEventListener('click', () => overlay.remove());
                  document.body.appendChild(overlay);
                }
                """,
                {"message": MANUAL_COMPLETION_MESSAGE},
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


def _first_confirmation_line(body_text: str) -> str:
    for line in body_text.splitlines():
        cleaned = line.strip()
        if cleaned and len(cleaned) > 6:
            return cleaned
    return "Application submitted."


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
        )
    )
