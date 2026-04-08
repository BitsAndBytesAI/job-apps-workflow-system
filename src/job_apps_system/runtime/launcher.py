from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from typing import Iterator

import uvicorn

from job_apps_system.config.settings import settings
from job_apps_system.runtime.bootstrap import BootstrapSummary, run_bootstrap
from job_apps_system.runtime.paths import ensure_runtime_directories


LOCK_FILE_NAME = "backend.lock"
LOG_FILE_NAME = "backend.log"


class BackendAlreadyRunningError(RuntimeError):
    pass


@contextmanager
def backend_instance_lock(app_data_dir: Path) -> Iterator[Path]:
    lock_path = app_data_dir / LOCK_FILE_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w+")

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise BackendAlreadyRunningError("Backend is already running.") from exc

    handle.write(str(Path(sys.executable)))
    handle.flush()

    try:
        yield lock_path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def configure_backend_logging(app_data_dir: Path) -> Path:
    runtime_dirs = ensure_runtime_directories(app_data_dir)
    log_path = runtime_dirs["logs"] / LOG_FILE_NAME

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True

    return log_path


def launch_backend(*, check_only: bool = False, host: str | None = None, port: int | None = None) -> int:
    summary = run_bootstrap()
    if check_only or not summary.ok:
        _print_bootstrap_summary(summary)
        return 0 if summary.ok else 1

    app_data_dir = settings.resolved_app_data_dir
    log_path = configure_backend_logging(app_data_dir)
    logging.getLogger(__name__).info("Starting backend launcher. log_file=%s", log_path)

    try:
        with backend_instance_lock(app_data_dir):
            config = uvicorn.Config(
                "job_apps_system.main:app",
                host=host or settings.app_host,
                port=port or settings.app_port,
                log_config=None,
                log_level=settings.log_level.lower(),
            )
            server = uvicorn.Server(config)
            try:
                server.run()
            except KeyboardInterrupt:
                logging.getLogger(__name__).info("Backend launcher interrupted; shutting down cleanly.")
                return 0
    except BackendAlreadyRunningError as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 2

    return 0


def _print_bootstrap_summary(summary: BootstrapSummary) -> None:
    payload = summary.to_dict()
    stream = sys.stdout if summary.ok else sys.stderr
    print(json.dumps(payload, indent=2), file=stream)
