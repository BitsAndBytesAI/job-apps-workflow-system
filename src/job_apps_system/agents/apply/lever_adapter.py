from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from job_apps_system.agents.apply.action_map import normalized_text
from job_apps_system.agents.apply.ai_browser_loop import (
    AmbiguousSubmitState,
    AiBrowserApplyLoop,
    MAX_TOTAL_ACTIONS,
    ManualHandoffRequested,
    NeedsReviewRequested,
    _submit_button_priority,
)
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.schemas.apply import ApplyAction, ApplyJobResult
from job_apps_system.services.application_answer_service import ApplicationAnswerService
from job_apps_system.services.apply_site_sessions import ApplySiteCredential


logger = logging.getLogger(__name__)


class LeverApplyAdapter:
    ats_type = "lever"

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
        site_credential: ApplySiteCredential | None = None,
    ) -> ApplyJobResult:
        logger.info(
            "Lever adapter started. job_id=%s company=%s title=%s page_url=%s",
            job.id,
            job.company_name,
            job.job_title,
            page.url,
        )
        return LeverAiBrowserApplyLoop(retain_success_logs=True).apply(
            page=page,
            job=job,
            applicant=applicant,
            resume_path=resume_path,
            answer_service=answer_service,
            screenshot_path=screenshot_path,
            auto_submit=auto_submit,
            detected_ats=self.ats_type,
            site_credential=site_credential,
            initial_reason="Lever adapter delegated form completion to AI browser loop.",
            manual_resume_url=job.apply_url,
            cancel_checker=cancel_checker,
        )


class LeverAiBrowserApplyLoop(AiBrowserApplyLoop):
    ats_type = LeverApplyAdapter.ats_type

    def _check_missing_required_consent_checkboxes(
        self,
        *,
        page: Page,
        observation: dict[str, object],
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
        steps: list[str],
    ) -> bool:
        # Lever exposes bottom-of-form consent checkboxes as soon as the page loads.
        # Do not check them from the generic observation hook; handle them only
        # immediately before a submit action so the flow still proceeds top-to-bottom.
        return False

    def _execute_action(
        self,
        *,
        page: Page,
        action: ApplyAction,
        targets: dict[str, dict[str, object]],
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
        site_credential: ApplySiteCredential | None = None,
    ) -> dict[str, object]:
        action_type = normalized_text(action.action).replace(" ", "_")
        if self._is_lever_page(page):
            target = targets.get(action.element_id or "")
            if action_type == "select" and self._is_missing_required_consent_checkbox(target):
                return {
                    "status": "skipped",
                    "message": "lever_submit_consent_deferred_until_submit",
                    "target": target,
                }
        if action_type == "submit_application" and self._is_lever_page(page):
            self._repair_known_yes_no_answers_before_submit(page)
            targets = self._fresh_targets(page, targets)
            self._refresh_resume_upload_before_submit(
                page=page,
                targets=targets,
                resume_path=resume_path,
                screenshot_path=screenshot_path,
                auto_submit=auto_submit,
            )
            targets = self._fresh_targets(page, targets)
            self._select_missing_required_consent_checkboxes(
                page=page,
                targets=targets,
                resume_path=resume_path,
                screenshot_path=screenshot_path,
                auto_submit=auto_submit,
            )
            targets = self._fresh_targets(page, targets)
            action = self._submit_action_for_targets(action, targets)
        return super()._execute_action(
            page=page,
            action=action,
            targets=targets,
            resume_path=resume_path,
            screenshot_path=screenshot_path,
            auto_submit=auto_submit,
            site_credential=site_credential,
        )

    def _fresh_targets(
        self,
        page: Page,
        fallback_targets: dict[str, dict[str, object]],
    ) -> dict[str, dict[str, object]]:
        try:
            observation = self._observe(page, detected_ats=self.ats_type)
            fresh_targets = self._target_map(observation)
        except PlaywrightError:
            return fallback_targets
        return fresh_targets or fallback_targets

    def _submit_action_for_targets(
        self,
        action: ApplyAction,
        targets: dict[str, dict[str, object]],
    ) -> ApplyAction:
        target = targets.get(action.element_id or "") or {}
        if (
            target.get("kind") == "button"
            and not target.get("disabled")
            and self._is_submit_target(target)
            and not self._is_application_entry_target(target, targets)
        ):
            return action

        candidates = [
            target
            for target in targets.values()
            if target.get("kind") == "button"
            and not target.get("disabled")
            and self._is_submit_target(target)
            and not self._is_application_entry_target(target, targets)
        ]
        if not candidates:
            return action
        candidates.sort(key=_submit_button_priority)
        target = candidates[0]
        return ApplyAction(
            action=action.action,
            element_id=str(target.get("id") or ""),
            value=action.value,
            field_name=action.field_name,
            reasoning=action.reasoning,
            confidence=action.confidence,
        )

    def _repair_known_yes_no_answers_before_submit(self, page: Page) -> int:
        applicant = self._current_applicant
        if applicant is None or not self._is_lever_page(page):
            return 0
        try:
            changed = int(
                page.evaluate(
                    """
                    ({ workAuthorized, requiresSponsorship }) => {
                      const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const isVisible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 &&
                          rect.height > 0 &&
                          style.display !== 'none' &&
                          style.visibility !== 'hidden' &&
                          !el.disabled;
                      };
                      const visibleRadios = Array.from(document.querySelectorAll('input[type="radio"]'))
                        .filter(isVisible);
                      const labelText = (input) => {
                        const parts = [];
                        if (input.labels) {
                          Array.from(input.labels).forEach((label) => {
                            parts.push(label.innerText || label.textContent || '');
                          });
                        }
                        parts.push(input.getAttribute('aria-label') || '');
                        parts.push(input.value || '');
                        return normalize(parts.join(' '));
                      };
                      const optionMatches = (input, desired) => {
                        const label = labelText(input);
                        if (desired) {
                          return /(^|\\b)(yes|true)(\\b|$)/.test(label);
                        }
                        return /(^|\\b)(no|false)(\\b|$)/.test(label);
                      };
                      const desiredForQuestion = (text) => {
                        const question = normalize(text);
                        if (!question) return null;
                        if (
                          (
                            question.includes('authorized to work') ||
                            question.includes('legally authorized') ||
                            question.includes('legal right to work') ||
                            question.includes('eligible to work')
                          ) &&
                          !question.includes('sponsor')
                        ) {
                          return Boolean(workAuthorized);
                        }
                        if (
                          question.includes('sponsor') ||
                          question.includes('visa sponsorship') ||
                          question.includes('immigration sponsorship') ||
                          question.includes('company-sponsored')
                        ) {
                          return Boolean(requiresSponsorship);
                        }
                        if (
                          question.includes('at least 18') ||
                          question.includes('18 years of age') ||
                          question.includes('18 years old') ||
                          question.includes('older than 18') ||
                          question.includes('age of majority')
                        ) {
                          return true;
                        }
                        return null;
                      };
                      const questionTextFor = (input, groupRadios) => {
                        const fieldset = input.closest('fieldset');
                        const legend = fieldset?.querySelector('legend');
                        const candidates = [];
                        if (legend) candidates.push(legend.innerText || legend.textContent || '');

                        for (let node = input.parentElement, depth = 0; node && node !== document.body && depth < 8; node = node.parentElement, depth += 1) {
                          const text = node.innerText || node.textContent || '';
                          const normalized = normalize(text);
                          if (
                            normalized &&
                            normalized.length <= 1400 &&
                            groupRadios.every((radio) => node.contains(radio)) &&
                            desiredForQuestion(normalized) !== null
                          ) {
                            candidates.push(text);
                            break;
                          }
                        }

                        for (let node = input.parentElement, depth = 0; node && node !== document.body && depth < 6; node = node.parentElement, depth += 1) {
                          let previous = node.previousElementSibling;
                          for (let index = 0; previous && index < 4; index += 1, previous = previous.previousElementSibling) {
                            const text = previous.innerText || previous.textContent || '';
                            const normalized = normalize(text);
                            if (normalized && normalized.length <= 600 && desiredForQuestion(normalized) !== null) {
                              candidates.push(text);
                            }
                          }
                        }

                        candidates.sort((left, right) => normalize(left).length - normalize(right).length);
                        return candidates[0] || '';
                      };

                      const groups = new Map();
                      visibleRadios.forEach((radio, index) => {
                        const key = radio.name || `radio-${index}`;
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(radio);
                      });

                      let changed = 0;
                      groups.forEach((groupRadios) => {
                        if (groupRadios.length < 2) return;
                        const questionText = questionTextFor(groupRadios[0], groupRadios);
                        const desired = desiredForQuestion(questionText);
                        if (desired === null) return;
                        const target = groupRadios.find((radio) => optionMatches(radio, desired));
                        if (!target || target.checked) return;
                        target.click();
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        changed += 1;
                      });
                      return changed;
                    }
                    """,
                    {
                        "workAuthorized": bool(applicant.work_authorized_us),
                        "requiresSponsorship": bool(applicant.requires_sponsorship),
                    },
                )
            )
        except (PlaywrightError, TypeError, ValueError):
            return 0
        if changed > 0:
            logger.info("Corrected %s Lever compliance yes/no answer(s) before submit.", changed)
            try:
                page.wait_for_timeout(300)
            except PlaywrightError:
                pass
        return changed

    def _refresh_resume_upload_before_submit(
        self,
        *,
        page: Page,
        targets: dict[str, dict[str, object]],
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
    ) -> bool:
        if self._total_actions >= MAX_TOTAL_ACTIONS or not self._is_lever_page(page):
            return False

        target = self._resume_upload_target(targets)
        if target is None:
            return False

        action = ApplyAction(
            action="upload_resume",
            element_id=str(target.get("id") or ""),
            reasoning="Lever can clear the resume after verification retries; refresh it immediately before submit.",
            confidence=0.99,
        )
        try:
            result = super()._execute_action(
                page=page,
                action=action,
                targets=targets,
                resume_path=resume_path,
                screenshot_path=screenshot_path,
                auto_submit=auto_submit,
            )
        except PlaywrightError:
            result = {"status": "failed", "message": "lever_resume_refresh_error", "target": target}
        self._total_actions += 1
        self._log_action(action, result["status"], result["message"], result.get("target"))

        if result["status"] == "success":
            logger.info("Refreshed Lever resume upload immediately before submit.")
            return True
        return False

    @classmethod
    def _resume_upload_target(cls, targets: dict[str, dict[str, object]]) -> dict[str, object] | None:
        candidates = [target for target in targets.values() if cls._is_resume_upload_target(target)]
        if not candidates:
            return None
        candidates.sort(key=lambda target: (0 if target.get("required") else 1, str(target.get("id") or "")))
        return candidates[0]

    @staticmethod
    def _is_resume_upload_target(target: dict[str, object]) -> bool:
        if target.get("kind") != "field" or target.get("disabled"):
            return False
        if str(target.get("type") or "").lower() != "file":
            return False

        label = normalized_text(
            " ".join(
                str(target.get(key) or "")
                for key in ("label", "placeholder", "name", "id", "value", "current_value")
            )
        )
        if any(term in label for term in ("cover letter", "portfolio", "work sample", "writing sample")):
            return False
        return any(term in label for term in ("resume", "resume/cv", "cv", "attach resume", "upload resume"))

    def _select_missing_required_consent_checkboxes(
        self,
        *,
        page: Page,
        targets: dict[str, dict[str, object]],
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
    ) -> int:
        if self._total_actions >= MAX_TOTAL_ACTIONS or not self._is_lever_page(page):
            return 0

        candidates = [
            target
            for target in targets.values()
            if self._is_missing_required_consent_checkbox(target)
        ]
        if not candidates:
            return 0

        candidates.sort(key=lambda target: (0 if target.get("invalid") else 1, str(target.get("id") or "")))
        checked_count = 0
        for target in candidates[:4]:
            if self._total_actions >= MAX_TOTAL_ACTIONS:
                break
            action = ApplyAction(
                action="select",
                element_id=str(target.get("id") or ""),
                value=True,
                reasoning="Lever required submit consent checkbox was still unchecked before submit.",
                confidence=0.99,
            )
            try:
                result = super()._execute_action(
                    page=page,
                    action=action,
                    targets=targets,
                    resume_path=resume_path,
                    screenshot_path=screenshot_path,
                    auto_submit=auto_submit,
                )
            except PlaywrightError:
                result = {"status": "failed", "message": "lever_consent_checkbox_selection_error", "target": target}
            self._total_actions += 1
            self._log_action(action, result["status"], result["message"], result.get("target"))
            if result["status"] == "success":
                checked_count += 1

        if checked_count > 0:
            logger.info("Checked %s missing Lever submit consent checkbox(es) before submit.", checked_count)
            try:
                page.wait_for_timeout(300)
            except PlaywrightError:
                pass
        return checked_count

    def _submit_if_plan_says_ready(
        self,
        *,
        page: Page,
        observation: dict[str, object],
        plan_message: str,
        resume_path: Path,
        screenshot_path: Path,
        auto_submit: bool,
        steps: list[str],
    ) -> bool:
        if not auto_submit or self._total_actions >= MAX_TOTAL_ACTIONS or not self._is_lever_page(page):
            return False
        if not self._plan_message_indicates_ready_to_submit(plan_message):
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
            reasoning="Lever planner said the application is complete and ready to submit.",
            confidence=0.96,
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
        self._record_step(steps, "Submitted completed Lever application after planner reported it was ready.")
        return True

    @staticmethod
    def _is_missing_required_consent_checkbox(target: dict[str, object]) -> bool:
        if target.get("kind") != "field" or target.get("disabled"):
            return False
        if str(target.get("type") or "").lower() != "checkbox":
            return False
        if target.get("checked") is True:
            return False

        label = normalized_text(
            " ".join(
                str(target.get(key) or "")
                for key in ("label", "placeholder", "name", "id", "value", "current_value")
            )
        )
        if not label:
            return False

        is_required = bool(target.get("required") or target.get("invalid"))
        if "submit application" in label:
            return True
        consent_terms = (
            "submit application",
            "i agree",
            "i certify",
            "certify that all statements",
            "privacy policy",
            "applicant privacy",
            "terms of use",
            "true and complete",
        )
        if is_required and any(term in label for term in consent_terms):
            return True

        return any(
            phrase in label
            for phrase in (
                "by clicking submit application",
                "certify that all statements made in this application",
                "i agree to the applicant privacy policy",
            )
        )

    @staticmethod
    def _plan_message_indicates_ready_to_submit(message: str) -> bool:
        normalized = normalized_text(message)
        if not normalized:
            return False
        if any(
            phrase in normalized
            for phrase in (
                "not ready to submit",
                "cannot submit",
                "can't submit",
                "needs manual",
                "manual step",
                "requires manual",
                "needs review",
                "requires review",
            )
        ):
            return False
        return any(
            phrase in normalized
            for phrase in (
                "ready to submit",
                "ready for submission",
                "form is complete",
                "application form is complete",
                "all required fields are filled",
                "all required fields filled",
            )
        )

    @staticmethod
    def _is_lever_page(page: Page) -> bool:
        try:
            if _is_lever_url(page.url):
                return True
            return any(_is_lever_url(frame.url) for frame in page.frames)
        except PlaywrightError:
            return False


def _is_lever_url(url: str | None) -> bool:
    host = urlparse(url or "").netloc.lower()
    return host == "lever.co" or host.endswith(".lever.co")


__all__ = ["LeverApplyAdapter", "LeverAiBrowserApplyLoop"]
