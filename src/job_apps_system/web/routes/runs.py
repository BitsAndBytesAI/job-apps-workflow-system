from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from job_apps_system.db.session import get_db_session
from job_apps_system.services.manual_runs import get_run as get_run_record
from job_apps_system.services.manual_runs import list_runs as list_run_records
from job_apps_system.services.manual_runs import request_run_cancel
from job_apps_system.services.setup_config import load_setup_config


router = APIRouter()


@router.get("/")
def list_runs() -> dict[str, list]:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        runs = list_run_records(session, project_id=project_id)
    summaries = [
        {
            "id": run["id"],
            "agent_name": run["agent_name"],
            "trigger_type": run["trigger_type"],
            "status": run["status"],
            "message": run["message"],
            "summary": _summarize_run(run),
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        }
        for run in runs
    ]
    return {"runs": summaries}


@router.get("/{run_id}")
def get_run_details(run_id: str) -> dict:
    with get_db_session() as session:
        run = get_run_record(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.post("/{run_id}/cancel")
def cancel_run(run_id: str) -> dict:
    with get_db_session() as session:
        run = get_run_record(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        if run["status"] not in {"queued", "running"}:
            raise HTTPException(status_code=400, detail="Run is not active.")

    cancelled = request_run_cancel(run_id)
    if cancelled is None:
        raise HTTPException(status_code=404, detail="Run is no longer active.")
    return cancelled


def _summarize_run(run: dict[str, Any]) -> str:
    result = run.get("result")
    if run.get("agent_name") == "job_intake" and isinstance(result, dict):
        parts: list[str] = []
        metrics = [
            ("scraped_count", "Scraped"),
            ("accepted_count", "Accepted"),
            ("processed_count", "Processed"),
            ("duplicate_count", "Duplicates"),
            ("filtered_count", "Filtered"),
        ]
        for key, label in metrics:
            if key in result and result[key] is not None:
                parts.append(f"{label} {result[key]}")
        if parts:
            return " · ".join(parts)
    if run.get("agent_name") == "resume_generation" and isinstance(result, dict):
        parts: list[str] = []
        metrics = [
            ("provider", "Provider"),
            ("model", "Model"),
            ("pending_jobs", "Pending"),
            ("attempted_count", "Attempted"),
            ("generated_count", "Generated"),
            ("failed_count", "Failed"),
        ]
        for key, label in metrics:
            if key in result and result[key] is not None:
                parts.append(f"{label} {result[key]}")
        if parts:
            return " · ".join(parts)
    if run.get("agent_name") == "job_scoring" and isinstance(result, dict):
        parts: list[str] = []
        metrics = [
            ("provider", "Provider"),
            ("model", "Model"),
            ("pending_jobs", "Pending"),
            ("attempted_count", "Attempted"),
            ("scored_count", "Scored"),
            ("failed_count", "Failed"),
        ]
        for key, label in metrics:
            if key in result and result[key] is not None:
                parts.append(f"{label} {result[key]}")
        if parts:
            return " · ".join(parts)
    if run.get("agent_name") == "job_apply" and isinstance(result, dict):
        parts: list[str] = []
        metrics = [
            ("pending_jobs", "Pending"),
            ("attempted_count", "Attempted"),
            ("applied_count", "Applied"),
            ("failed_count", "Failed"),
        ]
        for key, label in metrics:
            if key in result and result[key] is not None:
                parts.append(f"{label} {result[key]}")
        if parts:
            return " · ".join(parts)
    return str(run.get("message") or "")
