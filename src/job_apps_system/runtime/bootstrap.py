from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

from job_apps_system.config.settings import settings
from job_apps_system.runtime.paths import ensure_runtime_directories


REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "jinja2",
    "markdown",
    "sqlalchemy",
    "pydantic",
    "pydantic_settings",
    "googleapiclient",
    "google.auth",
    "google_auth_oauthlib",
    "playwright",
)


@dataclass
class BootstrapCheck:
    name: str
    ok: bool
    blocking: bool
    status: str
    message: str
    details: dict[str, Any] | None = None


@dataclass
class BootstrapSummary:
    ok: bool
    app_data_dir: str
    database_url: str
    log_file: str
    checks: list[BootstrapCheck]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "app_data_dir": self.app_data_dir,
            "database_url": self.database_url,
            "log_file": self.log_file,
            "checks": [asdict(check) for check in self.checks],
        }


def run_bootstrap() -> BootstrapSummary:
    checks: list[BootstrapCheck] = []
    app_data_dir = settings.resolved_app_data_dir
    log_file = app_data_dir / "logs" / "backend.log"

    checks.append(_check_python_runtime())
    checks.append(_check_python_dependencies())
    checks.append(_check_runtime_directories(app_data_dir))
    checks.append(_check_sqlite_runtime())
    checks.append(_check_database_initialization())
    checks.append(_check_google_oauth_config())
    checks.append(_check_google_chrome())

    ok = all(check.ok or not check.blocking for check in checks)
    return BootstrapSummary(
        ok=ok,
        app_data_dir=str(app_data_dir),
        database_url=settings.resolved_database_url,
        log_file=str(log_file),
        checks=checks,
    )


def _check_python_runtime() -> BootstrapCheck:
    executable = Path(sys.executable)
    ok = executable.exists()
    return BootstrapCheck(
        name="python_runtime",
        ok=ok,
        blocking=True,
        status="ready" if ok else "missing",
        message="Python runtime is available." if ok else "Python runtime is missing.",
        details={"executable": str(executable)},
    )


def _check_python_dependencies() -> BootstrapCheck:
    missing = [module for module in REQUIRED_MODULES if find_spec(module) is None]
    ok = not missing
    return BootstrapCheck(
        name="python_dependencies",
        ok=ok,
        blocking=True,
        status="ready" if ok else "missing",
        message="Required Python dependencies are available."
        if ok
        else "Required Python dependencies are missing.",
        details={"missing_modules": missing},
    )


def _check_runtime_directories(app_data_dir: Path) -> BootstrapCheck:
    created = ensure_runtime_directories(app_data_dir)
    return BootstrapCheck(
        name="runtime_directories",
        ok=True,
        blocking=True,
        status="ready",
        message="Runtime directories are ready.",
        details={key: str(value) for key, value in created.items()},
    )


def _check_sqlite_runtime() -> BootstrapCheck:
    try:
        sqlite_version = sqlite3.sqlite_version
        return BootstrapCheck(
            name="sqlite_runtime",
            ok=True,
            blocking=True,
            status="ready",
            message="SQLite runtime is available.",
            details={"version": sqlite_version},
        )
    except Exception as exc:
        return BootstrapCheck(
            name="sqlite_runtime",
            ok=False,
            blocking=True,
            status="missing",
            message=f"SQLite runtime is unavailable: {exc}",
            details=None,
        )


def _check_database_initialization() -> BootstrapCheck:
    try:
        from job_apps_system.db.session import init_db

        init_db()
        return BootstrapCheck(
            name="database_initialization",
            ok=True,
            blocking=True,
            status="ready",
            message="Database is initialized and migrations are applied.",
            details={"database_url": settings.resolved_database_url},
        )
    except Exception as exc:
        return BootstrapCheck(
            name="database_initialization",
            ok=False,
            blocking=True,
            status="failed",
            message=f"Database initialization failed: {exc}",
            details={"database_url": settings.resolved_database_url},
        )


def _check_google_oauth_config() -> BootstrapCheck:
    json_configured = bool((settings.google_oauth_client_config_json or "").strip())
    path_value = (settings.google_oauth_client_config_path or "").strip()
    path_exists = Path(path_value).expanduser().exists() if path_value else False
    ok = json_configured or path_exists
    return BootstrapCheck(
        name="google_oauth_config",
        ok=ok,
        blocking=False,
        status="ready" if ok else "warning",
        message="Google OAuth client config is available."
        if ok
        else "Google OAuth client config is not configured yet.",
        details={"configured_path": path_value or None, "path_exists": path_exists},
    )


def _check_google_chrome() -> BootstrapCheck:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    discovered = next((candidate for candidate in candidates if candidate.exists()), None)
    if discovered is None:
        chrome_bin = shutil.which("google-chrome") or shutil.which("chrome")
        discovered = Path(chrome_bin) if chrome_bin else None

    ok = discovered is not None and discovered.exists()
    return BootstrapCheck(
        name="google_chrome",
        ok=ok,
        blocking=False,
        status="ready" if ok else "warning",
        message="Google Chrome is available for LinkedIn automation."
        if ok
        else "Google Chrome is not installed or could not be found.",
        details={"path": str(discovered) if discovered else None},
    )
