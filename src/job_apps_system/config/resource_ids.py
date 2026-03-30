import re


GOOGLE_ID_PATTERNS = [
    re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/document/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/file/d/([a-zA-Z0-9-_]+)"),
    re.compile(r"/drive/folders/([a-zA-Z0-9-_]+)"),
]


def normalize_google_resource_id(value: str) -> str:
    for pattern in GOOGLE_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return value.strip()
