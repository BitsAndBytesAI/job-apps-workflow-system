from __future__ import annotations

import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ASHBY_HOST_SUFFIX = "ashbyhq.com"
DEFAULT_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
IGNORED_COMPANY_HOST_SUFFIXES = (
    "linkedin.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
)

_ASHBY_PUBLIC_WEBSITE_PATTERN = re.compile(r'"publicWebsite":"(https?:\\/\\/[^"]+)"')


def extract_company_domain(value: str | None, *, ignored_host_suffixes: tuple[str, ...] = IGNORED_COMPANY_HOST_SUFFIXES) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").strip().lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname or "." not in hostname:
        return None
    if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in ignored_host_suffixes):
        return None
    return hostname


def extract_domain_from_email(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if "@" not in normalized:
        return None
    domain = normalized.split("@", 1)[1].strip()
    if not domain or "." not in domain:
        return None
    return domain


def resolve_company_website_from_apply_url(apply_url: str | None) -> tuple[str | None, str | None]:
    if not _is_ashby_url(apply_url):
        return (None, None)

    req = Request(apply_url, headers=DEFAULT_REQUEST_HEADERS)
    with urlopen(req, timeout=30) as response:
        html = response.read().decode("utf-8", "ignore")

    website = extract_company_website_from_ashby_html(html)
    if not website:
        return (None, None)
    return (website, extract_company_domain(website, ignored_host_suffixes=()))


def extract_company_website_from_ashby_html(html: str) -> str | None:
    if not html:
        return None
    match = _ASHBY_PUBLIC_WEBSITE_PATTERN.search(html)
    if not match:
        return None
    return match.group(1).replace("\\/", "/").strip() or None


def is_ignored_company_host(value: str | None) -> bool:
    return extract_company_domain(value) is None


def _is_ashby_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname == ASHBY_HOST_SUFFIX or hostname.endswith(f".{ASHBY_HOST_SUFFIX}")
