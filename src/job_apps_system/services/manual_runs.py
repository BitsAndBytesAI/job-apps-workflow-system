from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.db.models.workflow_runs import WorkflowRun


FINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_ACTIVE_RUNS: dict[str, dict[str, Any]] = {}
_ACTIVE_RUNS_LOCK = Lock()


def create_manual_run(
    session: Session,
    *,
    agent_name: str,
    project_id: str | None = None,
    trigger_type: str = "manual",
    run_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_id = str(uuid4())
    started_at = datetime.now(timezone.utc)
    summary = {
        "agent_name": agent_name,
        "message": "Queued.",
        "steps": [],
        "result": None,
        "error": None,
        "cancel_requested": False,
        "run_payload": run_payload or {},
    }

    row = WorkflowRun(
        id=run_id,
        project_id=project_id,
        trigger_type=trigger_type,
        status="queued",
        started_at=started_at,
        finished_at=None,
        summary_json=json.dumps(summary),
    )
    session.add(row)
    session.flush()

    payload = serialize_run(row)
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = payload
    return payload


def update_active_run(
    run_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
    step_name: str | None = None,
    step_status: str | None = None,
    cancel_requested: bool | None = None,
) -> dict[str, Any] | None:
    with _ACTIVE_RUNS_LOCK:
        run = _ACTIVE_RUNS.get(run_id)
        if run is None:
            return None

        if status:
            run["status"] = status
        if message:
            run["message"] = message
        if cancel_requested is not None:
            run["cancel_requested"] = cancel_requested

        if step_name:
            steps = run.setdefault("steps", [])
            step = next((item for item in steps if item["name"] == step_name), None)
            timestamp = datetime.now(timezone.utc).isoformat()
            if step is None:
                step = {"name": step_name, "status": step_status or "running", "message": message or "", "updated_at": timestamp}
                steps.append(step)
            else:
                step["status"] = step_status or step["status"]
                if message is not None:
                    step["message"] = message
                step["updated_at"] = timestamp

        return dict(run)


def request_run_cancel(run_id: str) -> dict[str, Any] | None:
    return update_active_run(
        run_id,
        message="Cancellation requested. Waiting for the current step to stop.",
        cancel_requested=True,
    )


def is_run_cancel_requested(run_id: str) -> bool:
    with _ACTIVE_RUNS_LOCK:
        run = _ACTIVE_RUNS.get(run_id)
        if run is None:
            return False
        return bool(run.get("cancel_requested"))


def finalize_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    message: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    row = session.get(WorkflowRun, run_id)
    if row is None:
        raise ValueError(f"Workflow run {run_id} was not found.")

    with _ACTIVE_RUNS_LOCK:
        active = _ACTIVE_RUNS.pop(run_id, None)

    payload = active or serialize_run(row)
    payload["status"] = status
    payload["message"] = message
    payload["result"] = result
    payload["error"] = error
    payload["finished_at"] = datetime.now(timezone.utc).isoformat()

    row.status = status
    row.finished_at = datetime.now(timezone.utc)
    row.summary_json = json.dumps(
        {
            "agent_name": payload["agent_name"],
            "message": payload["message"],
            "steps": payload.get("steps", []),
            "result": result,
            "error": error,
            "cancel_requested": bool(payload.get("cancel_requested")),
            "run_payload": payload.get("run_payload") or {},
        }
    )
    session.flush()
    return payload


def get_run(session: Session, run_id: str) -> dict[str, Any] | None:
    with _ACTIVE_RUNS_LOCK:
        active = _ACTIVE_RUNS.get(run_id)
        if active is not None:
            return dict(active)

    row = session.get(WorkflowRun, run_id)
    if row is None:
        return None
    return serialize_run(row)


def activate_persisted_run(session: Session, run_id: str) -> dict[str, Any] | None:
    row = session.get(WorkflowRun, run_id)
    if row is None:
        return None
    payload = serialize_run(row)
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS[run_id] = dict(payload)
    return payload


def is_run_active_in_memory(run_id: str) -> bool:
    with _ACTIVE_RUNS_LOCK:
        return run_id in _ACTIVE_RUNS


def is_stale_run(run: dict[str, Any]) -> bool:
    if run.get("status") not in {"queued", "running"}:
        return False
    return not is_run_active_in_memory(str(run.get("id") or ""))


def list_stale_runs(session: Session, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
    return [run for run in list_runs(session, limit=limit, project_id=project_id) if is_stale_run(run)]


def list_runs(session: Session, limit: int = 50, project_id: str | None = None) -> list[dict[str, Any]]:
    query = select(WorkflowRun)
    if project_id:
        query = query.where(WorkflowRun.project_id == project_id)
    rows = session.scalars(
        query.order_by(WorkflowRun.started_at.desc().nullslast()).limit(limit)
    ).all()
    persisted = {row.id: serialize_run(row) for row in rows}

    with _ACTIVE_RUNS_LOCK:
        active = {run_id: dict(payload) for run_id, payload in _ACTIVE_RUNS.items()}

    merged = {**persisted, **active}
    return sorted(
        merged.values(),
        key=lambda item: item.get("started_at") or "",
        reverse=True,
    )


def serialize_run(row: WorkflowRun) -> dict[str, Any]:
    summary = _parse_summary(row.summary_json)
    return {
        "id": row.id,
        "agent_name": summary.get("agent_name") or row.trigger_type,
        "project_id": row.project_id,
        "trigger_type": row.trigger_type,
        "status": row.status,
        "message": summary.get("message", ""),
        "steps": summary.get("steps", []),
        "result": summary.get("result"),
        "error": summary.get("error"),
        "cancel_requested": bool(summary.get("cancel_requested")),
        "run_payload": summary.get("run_payload") if isinstance(summary.get("run_payload"), dict) else {},
        "started_at": _serialize_timestamp(row.started_at),
        "finished_at": _serialize_timestamp(row.finished_at),
    }


def _parse_summary(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
