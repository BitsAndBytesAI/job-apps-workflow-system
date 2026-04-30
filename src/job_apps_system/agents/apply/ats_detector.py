from __future__ import annotations

from urllib.parse import parse_qs, urlparse


ASHBY = "ashby"
GREENHOUSE = "greenhouse"
ICIMS = "icims"
DICE = "dice"
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
            for frame in page.frames:
                if _is_ashby_url(frame.url):
                    return ASHBY
                if _is_greenhouse_url(frame.url):
                    return GREENHOUSE
                if _is_icims_url(frame.url):
                    return ICIMS
                if _is_dice_url(frame.url):
                    return DICE
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
