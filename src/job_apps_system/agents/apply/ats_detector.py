from __future__ import annotations

from urllib.parse import parse_qs, urlparse


ASHBY = "ashby"
UNKNOWN = "unknown"


def detect_ats_type(url: str | None, page=None) -> str:
    if _is_ashby_url(url):
        return ASHBY

    if page is not None:
        try:
            if _is_ashby_url(page.url):
                return ASHBY
            for frame in page.frames:
                if _is_ashby_url(frame.url):
                    return ASHBY
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
