from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from job_apps_system.config.settings import settings
from job_apps_system.runtime.paths import resolve_runtime_path


DEFAULT_LINKEDIN_URL = "https://www.linkedin.com/feed/"


def resolve_browser_profile_path(profile_path: str | None) -> Path:
    return resolve_runtime_path(
        profile_path or "browser-profiles/linkedin",
        app_data_dir=settings.resolved_app_data_dir,
    )


def spawn_linkedin_browser(profile_path: str | None, start_url: str = DEFAULT_LINKEDIN_URL) -> dict[str, str | int | None | bool]:
    resolved_path = resolve_browser_profile_path(profile_path)
    resolved_path.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "job_apps_system.cli.launch_linkedin_browser",
            "--profile-path",
            str(resolved_path),
            "--start-url",
            start_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    return {
        "ok": True,
        "message": "Launched Chrome with the LinkedIn automation profile.",
        "profile_path": str(resolved_path),
        "pid": process.pid,
    }


def launch_linkedin_browser(profile_path: str | None, start_url: str = DEFAULT_LINKEDIN_URL) -> dict[str, str]:
    resolved_path = resolve_browser_profile_path(profile_path)
    resolved_path.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(resolved_path),
            channel="chrome",
            headless=False,
            args=["--window-size=1440,1100"],
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")
        page.bring_to_front()

        try:
            while True:
                time.sleep(1)
                if not context.pages:
                    break
        except PlaywrightError:
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass

    return {
        "status": "closed",
        "profile_path": str(resolved_path),
    }
