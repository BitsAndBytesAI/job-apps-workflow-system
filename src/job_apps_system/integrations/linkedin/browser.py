from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from job_apps_system.config.settings import settings
from job_apps_system.runtime.paths import resolve_runtime_path


DEFAULT_LINKEDIN_URL = "https://www.linkedin.com/feed/"
DEFAULT_BUNDLED_LINKEDIN_PROFILE = "browser-profiles/linkedin-bundled"
LEGACY_LINKEDIN_PROFILE = "browser-profiles/linkedin"
_LAUNCHED_BROWSER_PIDS: set[int] = set()


def resolve_browser_profile_path(profile_path: str | None) -> Path:
    return resolve_runtime_path(
        profile_path or DEFAULT_BUNDLED_LINKEDIN_PROFILE,
        app_data_dir=settings.resolved_app_data_dir,
    )


def launch_persistent_linkedin_context(playwright, profile_path: str | None, *, headless: bool):
    resolved_path = resolve_browser_profile_path(profile_path)
    resolved_path.mkdir(parents=True, exist_ok=True)

    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(resolved_path),
            headless=headless,
            args=["--window-size=1440,1100"],
            no_viewport=True,
        )
    except PlaywrightError as exc:
        message = str(exc)
        if "ProcessSingleton" in message:
            raise RuntimeError(
                "LinkedIn browser profile is already open. Close the automation browser before running intake."
            ) from exc
        raise

    return resolved_path, context


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
    _LAUNCHED_BROWSER_PIDS.add(process.pid)

    return {
        "ok": True,
        "message": "Launched the LinkedIn automation browser.",
        "profile_path": str(resolved_path),
        "pid": process.pid,
    }


def terminate_linkedin_browser(pid: int | None) -> dict[str, str | int | bool | None]:
    if pid is None:
        return {
            "ok": False,
            "message": "No LinkedIn browser process was provided.",
            "pid": None,
        }
    if pid not in _LAUNCHED_BROWSER_PIDS:
        return {
            "ok": False,
            "message": "That browser process is not managed by this app session.",
            "pid": pid,
        }

    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        _LAUNCHED_BROWSER_PIDS.discard(pid)
        return {
            "ok": True,
            "message": "LinkedIn browser was already closed.",
            "pid": pid,
        }
    except OSError as exc:
        return {
            "ok": False,
            "message": f"Unable to close the LinkedIn browser: {exc}",
            "pid": pid,
        }

    _LAUNCHED_BROWSER_PIDS.discard(pid)
    return {
        "ok": True,
        "message": "Closed the LinkedIn browser after session detection.",
        "pid": pid,
    }


def launch_linkedin_browser(profile_path: str | None, start_url: str = DEFAULT_LINKEDIN_URL) -> dict[str, str]:
    with sync_playwright() as playwright:
        resolved_path, context = launch_persistent_linkedin_context(
            playwright,
            profile_path,
            headless=False,
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
