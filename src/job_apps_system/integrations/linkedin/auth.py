from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from job_apps_system.integrations.linkedin.browser import resolve_browser_profile_path


LINKEDIN_COOKIE_NAMES = {"li_at", "JSESSIONID", "li_rm", "bscookie"}
CHROME_EPOCH_OFFSET_SECONDS = 11644473600


def get_linkedin_auth_status(profile_path: str | None) -> dict[str, bool | int | str]:
    resolved_path = resolve_browser_profile_path(profile_path)
    cookies_path = resolved_path / "Default" / "Cookies"

    if not cookies_path.exists():
        return {
            "ok": True,
            "authenticated": False,
            "message": "No browser cookie database found for this LinkedIn profile yet.",
            "profile_path": str(resolved_path),
            "cookie_count": 0,
        }

    try:
        with _open_cookie_db(cookies_path) as connection:
            rows = connection.execute(
                """
                select host_key, name, expires_utc
                from cookies
                where host_key like '%linkedin.com%'
                """
            ).fetchall()
    except sqlite3.DatabaseError as error:
        return {
            "ok": False,
            "authenticated": False,
            "message": f"Unable to inspect the LinkedIn cookie database: {error}",
            "profile_path": str(resolved_path),
            "cookie_count": 0,
        }

    now_seconds = datetime.now(timezone.utc).timestamp()
    authenticated = any(
        name == "li_at" and _is_cookie_unexpired(expires_utc, now_seconds)
        for _, name, expires_utc in rows
    )
    linkedin_cookie_count = sum(1 for _, name, _ in rows if name in LINKEDIN_COOKIE_NAMES)

    return {
        "ok": True,
        "authenticated": authenticated,
        "message": (
            "LinkedIn session detected in the browser profile."
            if authenticated
            else "No active LinkedIn session found in the browser profile."
        ),
        "profile_path": str(resolved_path),
        "cookie_count": linkedin_cookie_count,
    }


def _open_cookie_db(cookies_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{cookies_path}?mode=ro", uri=True)


def _is_cookie_unexpired(expires_utc: int, now_seconds: float) -> bool:
    if not expires_utc:
        return True
    expires_seconds = (expires_utc / 1_000_000) - CHROME_EPOCH_OFFSET_SECONDS
    return expires_seconds > now_seconds
