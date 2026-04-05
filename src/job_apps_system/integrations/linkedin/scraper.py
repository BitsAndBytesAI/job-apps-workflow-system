from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

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
