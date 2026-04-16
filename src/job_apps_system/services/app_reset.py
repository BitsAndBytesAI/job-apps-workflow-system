from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from job_apps_system.config.settings import settings
from job_apps_system.db.session import engine, init_db
from job_apps_system.runtime.paths import APP_SUPPORT_DIR_NAME, ensure_runtime_directories


@dataclass
class ResetSummary:
    app_data_dir: str
    removed_paths: list[str]


def reset_application_state() -> ResetSummary:
    app_data_dir = settings.resolved_app_data_dir.resolve()
    _assert_safe_app_data_dir(app_data_dir)

    removed_paths: list[str] = []
    engine.dispose()

    if app_data_dir.exists():
        for child in app_data_dir.iterdir():
            removed_paths.append(str(child))
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    ensure_runtime_directories(app_data_dir)
    init_db()

    return ResetSummary(
        app_data_dir=str(app_data_dir),
        removed_paths=removed_paths,
    )


def _assert_safe_app_data_dir(path: Path) -> None:
    normalized = path.resolve()
    if normalized.name != APP_SUPPORT_DIR_NAME:
        raise ValueError(f"Refusing to reset unexpected app data directory: {normalized}")
