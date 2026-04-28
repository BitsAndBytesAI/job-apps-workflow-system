from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from job_apps_system.services.job_apply_runner import start_job_apply_run
from job_apps_system.services.job_intake_runner import start_job_intake_run
from job_apps_system.services.job_scoring_runner import start_job_scoring_run
from job_apps_system.services.manual_runs import (
    finalize_run,
    get_run,
    is_stale_run,
    list_stale_runs,
)
from job_apps_system.services.resume_generation_runner import start_resume_generation_run


def stale_runs_for_project(session: Session, project_id: str | None) -> list[dict[str, Any]]:
    runs = list_stale_runs(session, project_id=project_id)
    return [_serialize_stale_run(run) for run in runs]


def cancel_stale_run(session: Session, run_id: str) -> dict[str, Any]:
    run = get_run(session, run_id)
    if run is None:
        raise ValueError("Run not found.")
    if not is_stale_run(run):
        raise ValueError("Run is not stale.")
    return finalize_run(
        session,
        run_id,
        status="cancelled",
        message="Cancelled stale run from previous app session.",
        error=None,
    )


def resume_stale_run(session: Session, run_id: str) -> tuple[dict[str, Any], str]:
    run = get_run(session, run_id)
    if run is None:
        raise ValueError("Run not found.")
    if not is_stale_run(run):
        raise ValueError("Run is not stale.")

    payload = _recovery_payload(run)
    if payload is None:
        raise ValueError("This stale run cannot be resumed because its original parameters were not saved.")

    agent_name = run["agent_name"]
    trigger_type = run.get("trigger_type") or "manual"
    if agent_name == "job_intake":
        resumed = start_job_intake_run(
            trigger_type=trigger_type,
            search_urls=payload.get("search_urls") or None,
            max_jobs_per_search=payload.get("max_jobs_per_search"),
            existing_run_id=run_id,
        )
    elif agent_name == "job_scoring":
        resumed = start_job_scoring_run(
            trigger_type=trigger_type,
            limit=payload.get("limit"),
            job_ids=payload.get("job_ids") or None,
            existing_run_id=run_id,
        )
    elif agent_name == "resume_generation":
        resumed = start_resume_generation_run(
            trigger_type=trigger_type,
            limit=payload.get("limit"),
            job_ids=payload.get("job_ids") or None,
            existing_run_id=run_id,
        )
    elif agent_name == "job_apply":
        resumed = start_job_apply_run(
            trigger_type=trigger_type,
            limit=payload.get("limit") or 1,
            job_ids=payload.get("job_ids") or None,
            existing_run_id=run_id,
        )
    else:
        raise ValueError(f"Unsupported stale run type: {agent_name}")

    return resumed, _resume_target(agent_name)


def _serialize_stale_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run["id"],
        "agent_name": run["agent_name"],
        "trigger_type": run["trigger_type"],
        "status": run["status"],
        "message": run["message"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "resumable": _recovery_payload(run) is not None,
        "resume_target": _resume_target(run["agent_name"]),
    }


def _recovery_payload(run: dict[str, Any]) -> dict[str, Any] | None:
    payload = run.get("run_payload")
    if isinstance(payload, dict) and payload:
        return payload

    agent_name = run.get("agent_name")
    if agent_name == "job_intake":
        return {"search_urls": [], "max_jobs_per_search": None}
    if agent_name == "job_scoring":
        return {"limit": None, "job_ids": []}
    if agent_name == "resume_generation":
        return {"limit": None, "job_ids": []}
    if agent_name == "job_apply":
        if isinstance(payload, dict) and payload.get("job_ids"):
            return payload
        return None
    return None


def _resume_target(agent_name: str) -> str:
    if agent_name == "job_intake":
        return "/jobs/all/"
    return "/jobs/"
