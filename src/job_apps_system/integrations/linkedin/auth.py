from __future__ import annotations

import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from job_apps_system.integrations.linkedin.browser import resolve_browser_profile_path


LINKEDIN_COOKIE_NAMES = {"li_at", "JSESSIONID", "li_rm", "bscookie"}
CHROME_EPOCH_OFFSET_SECONDS = 11644473600


def get_linkedin_auth_status(profile_path: str | None) -> dict[str, bool | int | str]:
    resolved_path = resolve_browser_profile_path(profile_path)
    cookie_store = _detect_cookie_store(resolved_path)

    if cookie_store is None:
        return {
            "ok": True,
            "authenticated": False,
            "message": "No browser cookie database found for this LinkedIn profile yet.",
            "profile_path": str(resolved_path),
            "cookie_count": 0,
        }

    try:
        with _open_cookie_db(cookie_store["path"]) as connection:
            rows = _load_linkedin_cookie_rows(connection, cookie_store["browser"])
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
    readable_path = _copy_cookie_db_if_needed(cookies_path)
    return sqlite3.connect(f"file:{readable_path}?mode=ro", uri=True)


def _detect_cookie_store(resolved_path: Path) -> dict[str, str | Path] | None:
    chromium_path = resolved_path / "Default" / "Cookies"
    if chromium_path.exists():
        return {"browser": "chromium", "path": chromium_path}

    firefox_path = resolved_path / "cookies.sqlite"
    if firefox_path.exists():
        return {"browser": "firefox", "path": firefox_path}

    return None


def _load_linkedin_cookie_rows(connection: sqlite3.Connection, browser: str) -> list[tuple[str, str, int]]:
    if browser == "firefox":
        return connection.execute(
            """
            select host, name, expiry
            from moz_cookies
            where host like '%linkedin.com%'
            """
        ).fetchall()

    return connection.execute(
        """
        select host_key, name, expires_utc
        from cookies
        where host_key like '%linkedin.com%'
        """
    ).fetchall()


def _copy_cookie_db_if_needed(cookies_path: Path) -> Path:
    temp_dir = Path(tempfile.gettempdir()) / "job-apps-cookie-copies"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{cookies_path.stem}-{abs(hash(str(cookies_path)))}.sqlite"
    shutil.copy2(cookies_path, temp_path)
    return temp_path


def _is_cookie_unexpired(expires_utc: int, now_seconds: float) -> bool:
    if not expires_utc:
        return True
    if expires_utc > 10_000_000_000_000:
        expires_seconds = (expires_utc / 1_000_000) - CHROME_EPOCH_OFFSET_SECONDS
    elif expires_utc > 10_000_000_000:
        expires_seconds = expires_utc / 1_000
    else:
        expires_seconds = expires_utc
    return expires_seconds > now_seconds
