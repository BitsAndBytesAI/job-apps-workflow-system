from __future__ import annotations

from pathlib import Path


APP_SUPPORT_DIR_NAME = "JobAppsWorkflowSystem"
RUNTIME_SUBDIRECTORIES = ("browser-profiles", "cache", "logs", "debug")


def default_app_data_dir(*, app_env: str = "development", configured: str | None = None) -> Path:
    if configured:
        return Path(configured).expanduser().resolve()

    return (Path.home() / "Library" / "Application Support" / APP_SUPPORT_DIR_NAME).resolve()


def resolve_runtime_path(raw_path: str | None, *, app_data_dir: Path) -> Path:
    raw_value = (raw_path or "").strip()
    if not raw_value:
        return app_data_dir.resolve()

    path = Path(raw_value).expanduser()
    if path.is_absolute():
        return path.resolve()

    if not path.parts:
        return app_data_dir.resolve()

    return app_data_dir.joinpath(*path.parts).resolve()


def ensure_runtime_directories(app_data_dir: Path) -> dict[str, Path]:
    root = app_data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)

    created = {"root": root}
    for name in RUNTIME_SUBDIRECTORIES:
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        created[name] = directory

    return created


def sqlite_url_for_path(path: Path) -> str:
    return f"sqlite:///{path.resolve()}"


def resolve_database_url(database_url: str | None, *, app_data_dir: Path) -> str:
    if not database_url:
        return sqlite_url_for_path(app_data_dir / "app.db")

    return database_url
