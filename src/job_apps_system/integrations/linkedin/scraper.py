from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from job_apps_system.integrations.linkedin.browser import resolve_browser_profile_path
from job_apps_system.integrations.linkedin.parsers import clean_text, parse_linkedin_job_id
from job_apps_system.schemas.jobs import ScrapedJob


JOB_CARDS_REQUEST_MARKER = "/voyager/api/voyagerJobsDashJobCards"
FULL_RESULTS_COUNT = 25
PREFETCH_QUERY_ID = "voyagerJobsDashJobCards.11efe66ab8e00aabdc31cf0a7f095a32"


class LinkedInScraper:
    def __init__(self, profile_path: str) -> None:
        self._profile_path = resolve_browser_profile_path(profile_path)

    def scrape_search_urls(self, search_urls: list[str], max_jobs_per_search: int = 25) -> list[ScrapedJob]:
        if not search_urls:
            return []

        self._profile_path.mkdir(parents=True, exist_ok=True)
        scraped: list[ScrapedJob] = []

        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_path),
                    channel="chrome",
                    headless=True,
                    args=["--window-size=1440,1100"],
                    no_viewport=True,
                )
            except PlaywrightError as exc:
                message = str(exc)
                if "ProcessSingleton" in message:
                    raise RuntimeError(
                        "LinkedIn browser profile is already open. Close the automation browser before running intake."
                    ) from exc
                raise

            try:
                for search_url in search_urls:
                    page = context.new_page()
                    try:
                        scraped.extend(self._scrape_search_url(page, search_url, max_jobs_per_search))
                    finally:
                        page.close()
            finally:
                context.close()

        deduped: dict[str, ScrapedJob] = {}
        for job in scraped:
            deduped[job.id] = job
        return list(deduped.values())

    def resolve_apply_urls(self, jobs: list[ScrapedJob]) -> dict[str, int]:
        if not jobs:
            return {"resolved_external": 0, "easy_apply": 0, "detail_page": 0}

        self._profile_path.mkdir(parents=True, exist_ok=True)
        summary = {"resolved_external": 0, "easy_apply": 0, "detail_page": 0}

        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_path),
                    channel="chrome",
                    headless=True,
                    args=["--window-size=1440,1100"],
                    no_viewport=True,
                )
            except PlaywrightError as exc:
                message = str(exc)
                if "ProcessSingleton" in message:
                    raise RuntimeError(
                        "LinkedIn browser profile is already open. Close the automation browser before running intake."
                    ) from exc
                raise

            try:
                page = context.new_page()
                try:
                    for job in jobs:
                        resolved = self._resolve_apply_url(page, job.id)
                        job.apply_url = resolved
                        if resolved == self._job_detail_url(job.id):
                            if self._job_uses_easy_apply(page):
                                summary["easy_apply"] += 1
                            else:
                                summary["detail_page"] += 1
                        else:
                            summary["resolved_external"] += 1
                finally:
                    page.close()
            finally:
                context.close()

        return summary

    def _scrape_search_url(self, page, search_url: str, max_jobs_per_search: int) -> list[ScrapedJob]:
        request_state = self._bootstrap_search_session(page, search_url)
        headers = request_state["headers"]
        api_url = request_state["job_cards_url"]

        jobs: list[ScrapedJob] = []
        seen_ids: set[str] = set()
        total = max_jobs_per_search

        for start in range(0, max_jobs_per_search, FULL_RESULTS_COUNT):
            cards_payload = self._fetch_json(page, self._job_cards_url_for_start(api_url, start), headers)
            cards_data = cards_payload.get("data") or {}
            paging = cards_data.get("paging") or {}
            if paging.get("total"):
                total = min(max_jobs_per_search, int(paging["total"]))

            page_jobs = self._parse_job_cards(cards_payload, search_url)
            if not page_jobs:
                break

            job_ids = [job.id for job in page_jobs]
            descriptions = self._fetch_job_descriptions(page, headers, job_ids)
            for job in page_jobs:
                job.job_description = descriptions.get(job.id, "")
                job.job_posting_url = job.job_posting_url or self._job_detail_url(job.id)
                job.apply_url = job.apply_url or f"https://www.linkedin.com/jobs/view/{job.id}/"
                if job.id not in seen_ids:
                    seen_ids.add(job.id)
                    jobs.append(job)

            if len(jobs) >= total or len(page_jobs) < FULL_RESULTS_COUNT:
                break

        return jobs[:max_jobs_per_search]

    def _bootstrap_search_session(self, page, search_url: str) -> dict[str, dict | str]:
        captured_requests: list[tuple[str, dict[str, str]]] = []

        def on_request(request) -> None:
            if JOB_CARDS_REQUEST_MARKER not in request.url:
                return
            if "count=25" not in request.url:
                return
            captured_requests.append((request.url, request.headers))

        page.on("request", on_request)
        page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        self._ensure_authenticated(page)

        if not captured_requests:
            raise RuntimeError("LinkedIn job search API request was not observed for this search URL.")

        pagination_requests = [
            item for item in captured_requests if "currentJobId:" not in item[0] or "start=25" in item[0]
        ]
        request_url, request_headers = (pagination_requests or captured_requests)[0]
        headers = {
            "csrf-token": request_headers.get("csrf-token", ""),
            "x-restli-protocol-version": request_headers.get("x-restli-protocol-version", "2.0.0"),
            "accept": request_headers.get("accept", "application/vnd.linkedin.normalized+json+2.1"),
            "x-li-lang": request_headers.get("x-li-lang", "en_US"),
            "x-li-track": request_headers.get("x-li-track", ""),
        }

        if not headers["csrf-token"]:
            raise RuntimeError("LinkedIn CSRF token was not available for API scraping.")

        return {"job_cards_url": request_url, "headers": headers}

    def _ensure_authenticated(self, page) -> None:
        current_url = page.url.lower()
        if "/login" in current_url or "session_redirect" in current_url:
            raise RuntimeError("LinkedIn is not authenticated in the configured browser profile.")

    def _fetch_json(self, page, url: str, headers: dict[str, str]) -> dict:
        result = page.evaluate(
            """async ({url, headers}) => {
                const response = await fetch(url, { credentials: 'include', headers });
                const text = await response.text();
                return { status: response.status, text };
            }""",
            {"url": url, "headers": headers},
        )
        status = int(result.get("status") or 0)
        if status != 200:
            body = clean_text(result.get("text"))
            raise RuntimeError(f"LinkedIn API request failed with status {status}: {body[:200]}")
        return page.evaluate("(text) => JSON.parse(text)", result["text"])

    def _parse_job_cards(self, payload: dict, search_url: str) -> list[ScrapedJob]:
        data = payload.get("data") or {}
        elements = data.get("elements") or []
        included = payload.get("included") or []
        by_urn = {item.get("entityUrn"): item for item in included if isinstance(item, dict) and item.get("entityUrn")}

        jobs: list[ScrapedJob] = []
        for element in elements:
            job_union = element.get("jobCardUnion") or element.get("jobCard") or {}
            card_urn = job_union.get("*jobPostingCard")
            if not card_urn:
                continue
            card = by_urn.get(card_urn)
            if not card:
                continue

            job_id = parse_linkedin_job_id(card.get("jobPostingUrn"), card.get("entityUrn"))
            if not job_id:
                continue

            jobs.append(
                ScrapedJob(
                    id=job_id,
                    tracking_id=clean_text(card.get("trackingId")) or None,
                    company_name=self._text_value(card.get("primaryDescription")),
                    job_title=clean_text(card.get("jobPostingTitle") or self._text_value(card.get("title"))),
                    job_description="",
                    posted_date=self._posted_date(card),
                    job_posting_url=self._job_detail_url(job_id),
                    apply_url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                    company_url=clean_text(((card.get("logo") or {}).get("actionTarget"))) or None,
                    search_url=search_url,
                    location=self._text_value(card.get("secondaryDescription")) or None,
                    listed_text=self._listed_text(card) or None,
                )
            )

        return jobs

    def _fetch_job_descriptions(self, page, headers: dict[str, str], job_ids: list[str]) -> dict[str, str]:
        if not job_ids:
            return {}

        urns = [f"urn:li:fsd_jobPostingCard:({job_id},JOB_DETAILS)" for job_id in job_ids]
        encoded_urns = ",".join(quote(urn, safe="") for urn in urns)
        variables = (
            "(jobPostingDetailDescription_start:0,"
            "jobPostingDetailDescription_count:5,"
            f"jobCardPrefetchQuery:(jobUseCase:JOB_DETAILS,prefetchJobPostingCardUrns:List({encoded_urns}),count:5),"
            "jobDetailsContext:(isJobSearch:true))"
        )
        url = (
            "https://www.linkedin.com/voyager/api/graphql"
            f"?includeWebMetadata=true&variables={variables}&queryId={PREFETCH_QUERY_ID}"
        )

        payload = self._fetch_json(page, url, headers)
        included = payload.get("included") or []

        descriptions: dict[str, str] = {}
        for item in included:
            if item.get("$type") != "com.linkedin.voyager.dash.jobs.JobDescription":
                continue
            job_id = parse_linkedin_job_id(None, item.get("entityUrn"))
            if not job_id:
                continue
            description = self._text_value(item.get("descriptionText"))
            if description:
                descriptions[job_id] = description
        return descriptions

    def _job_cards_url_for_start(self, request_url: str, start: int) -> str:
        if re.search(r"([?&])start=\d+", request_url):
            return re.sub(r"([?&])start=\d+", rf"\1start={start}", request_url, count=1)
        separator = "&" if "?" in request_url else "?"
        return f"{request_url}{separator}start={start}"

    def _resolve_apply_url(self, page, job_id: str) -> str:
        detail_url = self._job_detail_url(job_id)
        page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        self._ensure_authenticated(page)

        button = page.locator("button.jobs-apply-button:visible, a.jobs-apply-button:visible").first
        if not button.is_visible():
            return detail_url

        label = clean_text(button.get_attribute("aria-label") or button.inner_text()).lower()
        if "easy apply" in label:
            return detail_url

        href = clean_text(button.get_attribute("href"))
        direct_target = self._normalize_apply_target(href, detail_url)
        if direct_target != detail_url:
            return direct_target

        self._prepare_apply_capture(page)
        popup = None
        popup_url = ""
        try:
            with page.expect_popup(timeout=8000) as popup_info:
                button.click(timeout=8000)
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=15000)
            popup.wait_for_timeout(1000)
            popup_url = popup.url
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1500)
        except PlaywrightError:
            return detail_url
        finally:
            if popup:
                popup.close()

        captured_urls = page.evaluate("() => window.__jobAppsCapturedApplyUrls || []")
        for candidate in [popup_url, *captured_urls, page.url]:
            resolved = self._normalize_apply_target(candidate, detail_url)
            if resolved != detail_url:
                return resolved

        return detail_url

    def _prepare_apply_capture(self, page) -> None:
        page.evaluate(
            """() => {
                window.__jobAppsCapturedApplyUrls = [];
                if (!window.__jobAppsOriginalOpen) {
                    window.__jobAppsOriginalOpen = window.open;
                    window.open = function(url, ...args) {
                        if (url) {
                            window.__jobAppsCapturedApplyUrls.push(String(url));
                        }
                        return window.__jobAppsOriginalOpen.call(window, url, ...args);
                    };
                }
            }"""
        )

    def _normalize_apply_target(self, candidate: str | None, detail_url: str) -> str:
        value = clean_text(candidate)
        if not value or value == "about:blank":
            return detail_url

        if value.startswith("/"):
            value = urljoin(detail_url, value)

        parsed = urlparse(value)
        if not parsed.scheme:
            return detail_url

        query = parse_qs(parsed.query)
        for key in (
            "url",
            "destRedirectUrl",
            "destUrl",
            "rx_url",
            "redirect",
            "redirectUrl",
            "redirect_uri",
            "target",
            "targetUrl",
            "destination",
            "dest",
        ):
            if query.get(key):
                return self._normalize_apply_target(query[key][0], detail_url)

        if "linkedin.com" in parsed.netloc:
            return detail_url

        return value

    def _job_detail_url(self, job_id: str) -> str:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"

    def _job_uses_easy_apply(self, page) -> bool:
        button = page.locator("button.jobs-apply-button:visible, a.jobs-apply-button:visible").first
        if not button.is_visible():
            return False
        label = clean_text(button.get_attribute("aria-label") or button.inner_text()).lower()
        return "easy apply" in label

    @staticmethod
    def _text_value(value) -> str:
        if isinstance(value, dict):
            return clean_text(value.get("text"))
        return clean_text(value)

    def _listed_text(self, card: dict) -> str:
        footer_items = card.get("footerItems") or []
        labels: list[str] = []
        for item in footer_items:
            text = self._text_value(item.get("text"))
            if text:
                labels.append(text)
                continue
            if item.get("timeAt"):
                timestamp = datetime.fromtimestamp(item["timeAt"] / 1000, tz=timezone.utc)
                labels.append(timestamp.date().isoformat())
        return clean_text(" · ".join(labels))

    def _posted_date(self, card: dict) -> str | None:
        footer_items = card.get("footerItems") or []
        for item in footer_items:
            if item.get("timeAt"):
                timestamp = datetime.fromtimestamp(item["timeAt"] / 1000, tz=timezone.utc)
                return timestamp.date().isoformat()
        return None
