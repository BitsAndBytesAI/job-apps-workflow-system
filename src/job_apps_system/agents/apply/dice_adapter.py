from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from job_apps_system.agents.apply.action_map import normalized_text
from job_apps_system.agents.apply.ai_browser_loop import AiBrowserApplyLoop
from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.schemas.apply import ApplyJobResult
from job_apps_system.services.application_answer_service import ApplicationAnswerService
from job_apps_system.services.apply_site_sessions import ApplySiteCredential


logger = logging.getLogger(__name__)

MAX_DICE_GATEWAY_ATTEMPTS = 5
MAX_DICE_START_APPLY_RETRIES = 1
MAX_DICE_PROFILE_CTA_CLICKS = 1
MAX_DICE_PROFILE_CTA_WAIT_MS = 12000
DICE_PROFILE_COMPLETION_MESSAGE = (
    "Dice is redirecting this application back to login/profile setup, which usually means the Dice candidate "
    "profile is not complete yet. Complete the Dice profile in this browser window, then click Resume AI. "
    "The saved Dice browser profile will be reused for future Dice applications."
)


class DiceApplyAdapter:
    ats_type = "dice"

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
        steps: list[str] = []
        canonical_job_url = self._canonical_job_url(page.url) or self._canonical_job_url(job.apply_url)
        logger.info(
            "Dice gateway started. job_id=%s company=%s title=%s page_url=%s canonical_job_url=%s",
            job.id,
            job.company_name,
            job.job_title,
            page.url,
            canonical_job_url,
        )

        try:
            self._check_cancelled(cancel_checker)
            if canonical_job_url is None:
                canonical_job_url = self._wait_for_dice_job_detail(page)
            if canonical_job_url is None:
                raise RuntimeError("Dice job detail URL was not resolved after redirect.")
            self._record_step(steps, f"Resolved Dice job detail URL: {canonical_job_url}")

            start_apply_retries = 0
            profile_cta_clicks = 0
            for attempt in range(1, MAX_DICE_GATEWAY_ATTEMPTS + 1):
                self._check_cancelled(cancel_checker)
                page = self._active_page(page)
                if page.is_closed():
                    return self._manual_closed(job, screenshot_path, steps, "Dice browser window was closed.")

                if not self._is_dice_page(page.url):
                    self._record_step(steps, f"Dice handed off to external application page: {page.url}")
                    return self._continue_with_ai(job, screenshot_path, steps, page)

                if self._is_dice_application_flow_url(page.url):
                    self._record_step(steps, f"Dice application flow opened: {page.url}")
                    return self._continue_with_ai(job, screenshot_path, steps, page)

                if self._is_dice_job_detail_url(page.url):
                    if self._click_dice_apply_now(page):
                        self._record_step(steps, f"Clicked Dice Apply Now on attempt {attempt}.")
                        self._wait_after_navigation(page)
                        continue
                    self._record_step(steps, "Dice job page did not expose a visible Apply Now button.")
                    return self._continue_with_ai(job, screenshot_path, steps, page)

                if self._looks_like_dice_profile_page(page):
                    self._record_step(steps, "Completing Dice profile prerequisite before returning to the job.")
                    profile_result = self._run_ai_profile_step(
                        page=page,
                        job=job,
                        applicant=applicant,
                        resume_path=resume_path,
                        answer_service=answer_service,
                        screenshot_path=screenshot_path,
                        auto_submit=auto_submit,
                        cancel_checker=cancel_checker,
                        site_credential=site_credential,
                    )
                    steps.extend(profile_result.steps[-6:])
                    page = self._active_page(page)
                    if profile_result.status == "manual_closed" and self._should_stop_for_manual_result(profile_result):
                        profile_result.steps = steps
                        return profile_result
                    if page.is_closed():
                        return self._manual_closed(job, screenshot_path, steps, "Dice browser window was closed during profile setup.")
                    if self._should_click_profile_cta(profile_result, profile_cta_clicks) and self._click_create_free_profile(page):
                        profile_cta_clicks += 1
                        self._record_step(steps, "Clicked Dice Create free profile to open the candidate profile form.")
                        self._wait_for_profile_cta_result(page, steps)
                        continue
                    if self._should_request_profile_completion(profile_result, start_apply_retries):
                        manual_result = self._await_dice_profile_completion(
                            page=page,
                            job=job,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            cancel_checker=cancel_checker,
                        )
                        if manual_result is not None:
                            return manual_result
                        start_apply_retries = 0
                        page = self._active_page(page)
                        continue
                    page, opened_start_apply = self._resume_dice_application(page, canonical_job_url, steps)
                    if opened_start_apply:
                        start_apply_retries += 1
                    continue

                if self._click_create_free_profile(page):
                    self._record_step(steps, "Opened Dice profile creation from the Dice landing page.")
                    self._wait_after_navigation(page)
                    continue

                if self._looks_like_dice_auth_page(page):
                    self._record_step(steps, "Completing Dice login or registration before returning to the job.")
                    auth_result = self._run_ai_profile_step(
                        page=page,
                        job=job,
                        applicant=applicant,
                        resume_path=resume_path,
                        answer_service=answer_service,
                        screenshot_path=screenshot_path,
                        auto_submit=True,
                        cancel_checker=cancel_checker,
                        site_credential=site_credential,
                    )
                    steps.extend(auth_result.steps[-6:])
                    page = self._active_page(page)
                    if auth_result.status == "manual_closed" and self._should_stop_for_manual_result(auth_result):
                        auth_result.steps = steps
                        return auth_result
                    if page.is_closed():
                        return self._manual_closed(job, screenshot_path, steps, "Dice browser window was closed during login or registration.")
                    if self._should_click_profile_cta(auth_result, profile_cta_clicks) and self._click_create_free_profile(page):
                        profile_cta_clicks += 1
                        self._record_step(steps, "Clicked Dice Create free profile to open the candidate profile form.")
                        self._wait_for_profile_cta_result(page, steps)
                        continue
                    if self._should_request_profile_completion(auth_result, start_apply_retries):
                        manual_result = self._await_dice_profile_completion(
                            page=page,
                            job=job,
                            screenshot_path=screenshot_path,
                            steps=steps,
                            cancel_checker=cancel_checker,
                        )
                        if manual_result is not None:
                            return manual_result
                        start_apply_retries = 0
                        page = self._active_page(page)
                        continue
                    page, opened_start_apply = self._resume_dice_application(page, canonical_job_url, steps)
                    if opened_start_apply:
                        start_apply_retries += 1
                    continue

                self._record_step(steps, f"Dice page state was not recognized: {page.url}")
                return self._continue_with_ai(job, screenshot_path, steps, page)

            self._record_step(steps, "Dice gateway attempt limit reached.")
            return self._continue_with_ai(job, screenshot_path, steps, page)
        except Exception as exc:
            if str(exc) == "Apply agent cancelled.":
                raise
            if page.is_closed() or _is_target_closed_error(exc):
                return self._manual_closed(job, screenshot_path, steps, "Dice browser window was closed.")
            captured_path = self._capture_screenshot(page, screenshot_path)
            self._record_step(steps, f"Dice gateway failed: {exc}")
            logger.exception(
                "Dice gateway failed. job_id=%s screenshot=%s steps=%s",
                job.id,
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

    def _run_ai_profile_step(
        self,
        *,
        page: Page,
        job: Job,
        applicant: ApplicantProfileConfig,
        resume_path: Path,
        answer_service: ApplicationAnswerService,
        screenshot_path: Path,
        auto_submit: bool,
        cancel_checker,
        site_credential: ApplySiteCredential | None,
    ) -> ApplyJobResult:
        loop = AiBrowserApplyLoop(retain_success_logs=True)
        return loop.apply(
            page=page,
            job=job,
            applicant=applicant,
            resume_path=resume_path,
            answer_service=answer_service,
            screenshot_path=screenshot_path,
            auto_submit=auto_submit,
            detected_ats="dice_profile",
            site_credential=site_credential,
            initial_reason="Complete Dice login, registration, or profile prerequisite before returning to the selected job.",
            cancel_checker=cancel_checker,
        )

    def _wait_for_dice_job_detail(self, page: Page) -> str | None:
        deadline_attempts = 12
        for _ in range(deadline_attempts):
            if self._is_dice_job_detail_url(page.url):
                return self._canonical_job_url(page.url)
            try:
                page.wait_for_timeout(500)
            except PlaywrightError:
                return None
        return self._canonical_job_url(page.url)

    def _return_to_dice_job(self, page: Page, canonical_job_url: str, steps: list[str]) -> None:
        if page.is_closed():
            return
        page.goto(canonical_job_url, wait_until="domcontentloaded", timeout=60000)
        self._wait_after_navigation(page)
        self._record_step(steps, "Returned to the original Dice job after account/profile step.")

    def _resume_dice_application(self, page: Page, canonical_job_url: str, steps: list[str]) -> tuple[Page, bool]:
        page = self._active_page(page)
        if page.is_closed():
            return page, False

        start_apply_url = self._dice_application_url_from_page(page, canonical_job_url)
        if start_apply_url:
            try:
                page.goto(start_apply_url, wait_until="domcontentloaded", timeout=60000)
                self._wait_after_navigation(page)
                page = self._active_page(page)
                self._record_step(steps, f"Opened Dice application start URL after account/profile step: {start_apply_url}")
                return page, True
            except PlaywrightError as exc:
                self._record_step(steps, f"Dice application start URL did not load cleanly: {exc}")

        self._return_to_dice_job(page, canonical_job_url, steps)
        return self._active_page(page), False

    def _click_dice_apply_now(self, page: Page) -> bool:
        return self._click_visible_button_or_link(page, (r"^apply now$", r"^apply$"))

    def _click_create_free_profile(self, page: Page) -> bool:
        return self._click_visible_button_or_link(
            page,
            (
                r"^create free profile$",
                r"^create profile$",
                r"^complete profile$",
                r"^create your profile$",
            ),
        )

    def _wait_for_profile_cta_result(self, page: Page, steps: list[str]) -> None:
        deadline = MAX_DICE_PROFILE_CTA_WAIT_MS // 500
        starting_url = page.url
        for _ in range(deadline):
            if page.is_closed():
                return
            page = self._active_page(page)
            if self._has_visible_candidate_profile_fields(page):
                self._record_step(steps, "Dice candidate profile form fields are visible after Create free profile.")
                return
            if page.url != starting_url and not self._looks_like_dice_profile_landing(page):
                self._record_step(steps, f"Dice profile CTA changed page state: {page.url}")
                return
            try:
                page.wait_for_timeout(500)
            except PlaywrightError:
                return
        self._record_step(steps, "Dice profile CTA did not reveal candidate profile fields before timeout.")

    def _has_visible_candidate_profile_fields(self, page: Page) -> bool:
        for frame in page.frames:
            try:
                count = frame.locator(
                    "input:not([type='hidden']):not([type='search']):not([type='button']):not([type='submit']), "
                    "textarea, select, [role='combobox']"
                ).evaluate_all(
                    """
                    els => els.filter((el) => {
                      const style = window.getComputedStyle(el);
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled;
                    }).length
                    """
                )
            except PlaywrightError:
                continue
            if int(count or 0) > 0:
                return True
        return False

    def _looks_like_dice_profile_landing(self, page: Page) -> bool:
        text = self._page_text(page)
        path = urlparse(page.url or "").path.lower()
        return path.startswith("/register") and "where tech connects" in text and "create free profile" in text

    def _click_visible_button_or_link(self, page: Page, patterns: tuple[str, ...]) -> bool:
        for pattern in patterns:
            regex = re.compile(pattern, re.IGNORECASE)
            for locator_factory in (page.get_by_role("button", name=regex), page.get_by_role("link", name=regex)):
                try:
                    count = min(locator_factory.count(), 8)
                except PlaywrightError:
                    continue
                for index in range(count):
                    locator = locator_factory.nth(index)
                    try:
                        if not locator.is_visible(timeout=500):
                            continue
                        locator.click(timeout=8000)
                        return True
                    except PlaywrightError:
                        continue
        return False

    def _looks_like_dice_auth_page(self, page: Page) -> bool:
        text = self._page_text(page)
        url = normalized_text(page.url)
        return any(
            token in text or token in url
            for token in (
                "login",
                "log in",
                "sign in",
                "register",
                "registration",
                "create account",
                "create an account",
                "password",
            )
        )

    def _looks_like_dice_profile_page(self, page: Page) -> bool:
        text = self._page_text(page)
        url = normalized_text(page.url)
        return any(
            token in text or token in url
            for token in (
                "create free profile",
                "create your profile",
                "complete your profile",
                "profile creation",
                "resume profile",
                "be seen and get hired",
                "tech professionals",
            )
        )

    def _page_text(self, page: Page) -> str:
        try:
            return normalized_text(page.locator("body").inner_text(timeout=1200))
        except PlaywrightError:
            return ""

    def _active_page(self, page: Page) -> Page:
        try:
            pages = page.context.pages
        except PlaywrightError:
            return page
        for candidate in reversed(pages):
            try:
                if not candidate.is_closed():
                    return candidate
            except PlaywrightError:
                continue
        return page

    def _should_stop_for_manual_result(self, result: ApplyJobResult) -> bool:
        text = normalized_text(" ".join(result.steps or []))
        return "user chose to finish" in text or "user cancelled" in text

    def _should_request_profile_completion(self, result: ApplyJobResult, start_apply_retries: int) -> bool:
        return self._is_profile_detour_yield(result) and start_apply_retries >= MAX_DICE_START_APPLY_RETRIES

    def _should_click_profile_cta(self, result: ApplyJobResult, profile_cta_clicks: int) -> bool:
        return self._is_profile_detour_yield(result) and profile_cta_clicks < MAX_DICE_PROFILE_CTA_CLICKS

    def _is_profile_detour_yield(self, result: ApplyJobResult) -> bool:
        text = normalized_text(" ".join([result.confirmation_text or "", *(result.steps or [])]))
        return "dice profile or registration detour has no actionable form fields" in text

    def _await_dice_profile_completion(
        self,
        *,
        page: Page,
        job: Job,
        screenshot_path: Path,
        steps: list[str],
        cancel_checker,
    ) -> ApplyJobResult | None:
        self._record_step(steps, "Dice requires manual profile completion before the application can continue.")
        loop = AiBrowserApplyLoop(retain_success_logs=True)
        return loop.await_manual_resolution(
            page=page,
            job=job,
            detected_ats="dice_profile",
            screenshot_path=screenshot_path,
            steps=steps,
            cancel_checker=cancel_checker,
            message=DICE_PROFILE_COMPLETION_MESSAGE,
        )

    def _dice_application_url_from_page(self, page: Page, canonical_job_url: str) -> str | None:
        for candidate in self._dice_url_candidates(page):
            resolved = self._dice_application_url_from_candidate(candidate)
            if resolved:
                return resolved
        return self._dice_start_apply_url(canonical_job_url)

    def _dice_url_candidates(self, page: Page) -> list[str]:
        candidates: list[str] = []
        try:
            candidates.append(page.url)
        except PlaywrightError:
            return candidates
        for frame in page.frames:
            try:
                candidates.append(frame.url)
            except PlaywrightError:
                continue
        try:
            hrefs = page.locator("a[href]").evaluate_all("els => els.map((el) => el.href || el.getAttribute('href') || '')")
            candidates.extend(str(item) for item in hrefs if item)
        except PlaywrightError:
            pass
        return candidates

    def _dice_application_url_from_candidate(self, url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        normalized_url = url
        if not parsed.netloc and str(url).startswith("/"):
            normalized_url = urljoin("https://www.dice.com", url)
            parsed = urlparse(normalized_url)

        if self._is_dice_application_flow_url(normalized_url):
            return self._strip_url_fragment(normalized_url)

        query = parse_qs(parsed.query)
        for key in ("redirectUrl", "redirect", "returnUrl", "next"):
            for raw_value in query.get(key, []):
                value = unquote(str(raw_value or ""))
                if not value:
                    continue
                if value.startswith("/"):
                    value = urljoin("https://www.dice.com", value)
                if self._is_dice_application_flow_url(value):
                    return self._strip_url_fragment(value)
        return None

    def _dice_start_apply_url(self, canonical_job_url: str | None) -> str | None:
        job_id = self._dice_job_id(canonical_job_url)
        if not job_id:
            return None
        return f"https://www.dice.com/job-applications/{job_id}/start-apply"

    def _dice_job_id(self, url: str | None) -> str | None:
        parsed = urlparse(url or "")
        match = re.search(r"/job-detail/([^/?#]+)", parsed.path, re.IGNORECASE)
        return match.group(1) if match else None

    def _is_dice_application_flow_url(self, url: str | None) -> bool:
        parsed = urlparse(url or "")
        return self._is_dice_page(url) and parsed.path.lower().startswith("/job-applications/")

    @staticmethod
    def _strip_url_fragment(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.fragment:
            return url
        return parsed._replace(fragment="").geturl()

    def _continue_with_ai(self, job: Job, screenshot_path: Path, steps: list[str], page: Page) -> ApplyJobResult:
        captured_path = self._capture_screenshot(page, screenshot_path)
        return ApplyJobResult(
            job_id=job.id,
            company_name=job.company_name,
            job_title=job.job_title,
            ats_type=self.ats_type,
            status="needs_review",
            success=False,
            screenshot_path=captured_path,
            confirmation_text="Dice gateway is complete; continue with AI browser apply loop.",
            steps=steps,
        )

    def _manual_closed(self, job: Job, screenshot_path: Path, steps: list[str], message: str) -> ApplyJobResult:
        self._record_step(steps, message)
        return ApplyJobResult(
            job_id=job.id,
            company_name=job.company_name,
            job_title=job.job_title,
            ats_type=self.ats_type,
            status="manual_closed",
            success=False,
            screenshot_path=str(screenshot_path) if screenshot_path.exists() else None,
            confirmation_text=message,
            steps=steps,
        )

    def _capture_screenshot(self, page: Page, screenshot_path: Path) -> str | None:
        if page.is_closed():
            return str(screenshot_path) if screenshot_path.exists() else None
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            return str(screenshot_path)
        except Exception:
            return str(screenshot_path) if screenshot_path.exists() else None

    def _wait_after_navigation(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            try:
                page.wait_for_timeout(1500)
            except PlaywrightError:
                pass

    def _canonical_job_url(self, url: str | None) -> str | None:
        if not self._is_dice_job_detail_url(url):
            return None
        parsed = urlparse(url or "")
        return f"{parsed.scheme or 'https'}://{parsed.netloc}{parsed.path}"

    def _is_dice_page(self, url: str | None) -> bool:
        parsed = urlparse(url or "")
        host = parsed.netloc.lower()
        return host == "dice.com" or host.endswith(".dice.com")

    def _is_dice_job_detail_url(self, url: str | None) -> bool:
        parsed = urlparse(url or "")
        return self._is_dice_page(url) and parsed.path.lower().startswith("/job-detail/")

    @staticmethod
    def _record_step(steps: list[str], message: str) -> None:
        steps.append(message)
        logger.info("Dice gateway step: %s", message)

    @staticmethod
    def _check_cancelled(cancel_checker) -> None:
        if cancel_checker and cancel_checker():
            raise RuntimeError("Apply agent cancelled.")


def _is_target_closed_error(exc: Exception) -> bool:
    return "target page, context or browser has been closed" in str(exc).lower()
