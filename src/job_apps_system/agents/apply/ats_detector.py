from __future__ import annotations

from urllib.parse import parse_qs, urlparse


ASHBY = "ashby"
GREENHOUSE = "greenhouse"
ICIMS = "icims"
DICE = "dice"
LINKEDIN = "linkedin"
ORACLE_CLOUD = "oracle_cloud"
WORKDAY = "workday"
UNKNOWN = "unknown"


def detect_ats_type(url: str | None, page=None) -> str:
    if _is_ashby_url(url):
        return ASHBY
    if _is_greenhouse_url(url):
        return GREENHOUSE
    if _is_icims_url(url):
        return ICIMS
    if _is_dice_url(url):
        return DICE
    if _is_linkedin_url(url):
        return LINKEDIN
    if _is_oracle_cloud_url(url):
        return ORACLE_CLOUD
    if _is_workday_url(url):
        return WORKDAY

    if page is not None:
        try:
            if _is_ashby_url(page.url):
                return ASHBY
            if _is_greenhouse_url(page.url):
                return GREENHOUSE
            if _is_icims_url(page.url):
                return ICIMS
            if _is_dice_url(page.url):
                return DICE
            if _is_linkedin_url(page.url):
                return LINKEDIN
            if _is_oracle_cloud_url(page.url):
                return ORACLE_CLOUD
            if _is_workday_url(page.url):
                return WORKDAY
            for frame in page.frames:
                if _is_ashby_url(frame.url):
                    return ASHBY
                if _is_greenhouse_url(frame.url):
                    return GREENHOUSE
                if _is_icims_url(frame.url):
                    return ICIMS
                if _is_dice_url(frame.url):
                    return DICE
                if _is_linkedin_url(frame.url):
                    return LINKEDIN
                if _is_oracle_cloud_url(frame.url):
                    return ORACLE_CLOUD
                if _is_workday_url(frame.url):
                    return WORKDAY
        except Exception:
            return UNKNOWN

    return UNKNOWN


def _is_ashby_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "jobs.ashbyhq.com" in host:
        return True
    query = parse_qs(parsed.query)
    return bool(query.get("ashby_jid"))


def _is_greenhouse_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "boards.greenhouse.io" in host or "job-boards.greenhouse.io" in host:
        return True
    query = parse_qs(parsed.query)
    return bool(query.get("gh_jid"))


def _is_icims_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "icims.com" not in host:
        return False
    return path.startswith("/jobs/")


def _is_dice_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return host == "dice.com" or host.endswith(".dice.com")


def _is_linkedin_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _is_oracle_cloud_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return "oraclecloud.com" in parsed.netloc.lower()


def _is_workday_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return "workdayjobs.com" in host or "myworkdayjobs.com" in host or "myworkdaysite.com" in host
