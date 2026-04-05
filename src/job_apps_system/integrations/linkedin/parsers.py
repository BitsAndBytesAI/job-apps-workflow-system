from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


JOB_VIEW_RE = re.compile(r"/jobs/view/(\d+)")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_linkedin_job_id(url: str | None, fallback: str | None = None) -> str | None:
    raw_url = clean_text(url)
    if raw_url:
        match = JOB_VIEW_RE.search(raw_url)
        if match:
            return match.group(1)

        parsed = urlparse(raw_url)
        current_job_id = parse_qs(parsed.query).get("currentJobId")
        if current_job_id and current_job_id[0]:
            return current_job_id[0]

    fallback_value = clean_text(fallback)
    if fallback_value:
        digits_only = re.sub(r"\D+", "", fallback_value)
        if digits_only:
            return digits_only
    return None
