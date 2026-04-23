from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from job_apps_system.config.settings import settings
from job_apps_system.schemas.schedule import LaunchAgentStatus


LAUNCH_AGENT_LABEL = "ai.bitsandbytes.jobapps.scheduler"
LAUNCH_AGENT_FILE_NAME = f"{LAUNCH_AGENT_LABEL}.plist"


def scheduler_launch_agent_status() -> LaunchAgentStatus:
    plist_path = launch_agent_plist_path()
    installed = plist_path.is_file()
    loaded = False
    message = "Scheduler background item is not installed."
    if installed:
        result = _run_launchctl(["print", launch_agent_domain_label()])
        loaded = result.returncode == 0 if result else False
        message = (
            "Scheduler background item is active."
            if loaded
            else "Scheduler background item is installed but not loaded."
        )
    return LaunchAgentStatus(
        label=LAUNCH_AGENT_LABEL,
        installed=installed,
        loaded=loaded,
        plist_path=str(plist_path),
        status_message=message,
    )


def install_scheduler_launch_agent() -> LaunchAgentStatus:
    plist_path = launch_agent_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(render_scheduler_launch_agent()))

    _run_launchctl(["bootout", launch_agent_domain_label()])
    bootstrap = _run_launchctl(["bootstrap", launch_agent_domain(), str(plist_path)])
    if bootstrap is None:
        raise RuntimeError("launchctl is not available in the current environment.")
    if bootstrap.returncode != 0:
        raise RuntimeError(bootstrap.stderr.strip() or bootstrap.stdout.strip() or "Unable to install the scheduler LaunchAgent.")
    return scheduler_launch_agent_status()


def uninstall_scheduler_launch_agent() -> LaunchAgentStatus:
    _run_launchctl(["bootout", launch_agent_domain_label()])
    plist_path = launch_agent_plist_path()
    if plist_path.exists():
        plist_path.unlink()
    return scheduler_launch_agent_status()


def render_scheduler_launch_agent() -> dict:
    logs_dir = settings.resolved_app_data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    program_arguments, environment = resolve_scheduler_command()
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": program_arguments,
        "StartInterval": 60,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(logs_dir / "scheduler-launchd.log"),
        "StandardErrorPath": str(logs_dir / "scheduler-launchd.log"),
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(settings.resolved_app_data_dir),
    }


def resolve_scheduler_command() -> tuple[list[str], dict[str, str]]:
    environment = {
        "APP_ENV": settings.app_env,
        "APP_DATA_DIR": str(settings.resolved_app_data_dir),
        "JOB_APPS_SECRET_BACKEND": os.getenv("JOB_APPS_SECRET_BACKEND", "python_native"),
        "PATH": os.getenv("PATH", ""),
    }
    if settings.database_url:
        environment["DATABASE_URL"] = settings.database_url
    if settings.google_oauth_client_config_path:
        environment["GOOGLE_OAUTH_CLIENT_CONFIG_PATH"] = settings.google_oauth_client_config_path
    if settings.google_oauth_client_config_json:
        environment["GOOGLE_OAUTH_CLIENT_CONFIG_JSON"] = settings.google_oauth_client_config_json
    if os.getenv("JOB_APPS_ALLOW_UNSIGNED_HELPER"):
        environment["JOB_APPS_ALLOW_UNSIGNED_HELPER"] = os.getenv("JOB_APPS_ALLOW_UNSIGNED_HELPER", "")

    if settings.app_env in {"packaged_debug", "packaged"}:
        scheduler_agent = resolve_packaged_scheduler_agent()
        helper_path = resolve_packaged_secret_helper()
        environment["APP_ENV"] = settings.app_env
        environment["JOB_APPS_SECRET_BACKEND"] = "native_helper"
        if helper_path:
            environment["JOB_APPS_SECRET_HELPER"] = str(helper_path)
        return [str(scheduler_agent)], environment

    repo_root = Path(__file__).resolve().parents[3]
    environment["PYTHONPATH"] = str(repo_root / "src")
    return [sys.executable, "-m", "job_apps_system.cli.scheduler_tick"], environment


def resolve_packaged_scheduler_agent() -> Path:
    explicit = (os.getenv("JOB_APPS_SCHEDULER_AGENT") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate.resolve()

    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix != ".app":
            continue
        candidate = parent / "Contents" / "Resources" / "JobAppsSchedulerAgent"
        if candidate.exists():
            return candidate.resolve()
    raise RuntimeError("Bundled JobAppsSchedulerAgent was not found.")


def resolve_packaged_secret_helper() -> Path | None:
    explicit = (os.getenv("JOB_APPS_SECRET_HELPER") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate.resolve()

    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix != ".app":
            continue
        candidate = parent / "Contents" / "Helpers" / "JobAppsSecretHelper.app" / "Contents" / "MacOS" / "JobAppsSecretHelper"
        if candidate.exists():
            return candidate.resolve()
    return None


def launch_agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / LAUNCH_AGENT_FILE_NAME


def launch_agent_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_agent_domain_label() -> str:
    return f"{launch_agent_domain()}/{LAUNCH_AGENT_LABEL}"


def _run_launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["launchctl", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
