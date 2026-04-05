from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


GOOGLE_ID_PATTERNS = [
    re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/document/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/file/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/folders/([a-zA-Z0-9-_]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9-_]+)"),
]


def normalize_google_resource_id(value: str) -> str:
    for pattern in GOOGLE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value.strip()


@dataclass(frozen=True)
class GoogleSheetReference:
    raw_value: str
    spreadsheet_id: str
    gid: int | None = None


def parse_google_sheet_reference(value: str) -> GoogleSheetReference:
    raw_value = value.strip()
    spreadsheet_id = normalize_google_resource_id(raw_value)
    gid = _extract_google_sheet_gid(raw_value)
    return GoogleSheetReference(raw_value=raw_value, spreadsheet_id=spreadsheet_id, gid=gid)


def _extract_google_sheet_gid(value: str) -> int | None:
    if not value:
        return None

    parsed = urlparse(value)
    query_gid = parse_qs(parsed.query).get("gid")
    if query_gid:
        return _parse_gid(query_gid[0])

    if parsed.fragment:
        fragment_gid = parse_qs(parsed.fragment).get("gid")
        if fragment_gid:
            return _parse_gid(fragment_gid[0])

        if parsed.fragment.startswith("gid="):
            return _parse_gid(parsed.fragment.split("=", 1)[1])

    return None


def _parse_gid(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
