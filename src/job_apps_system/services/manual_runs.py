from __future__ import annotations

import logging
import json
import time
from datetime import datetime, timezone
from threading import Condition, Lock, Thread
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from job_apps_system.db.models.workflow_runs import WorkflowRun
from job_apps_system.db.session import SessionLocal


FINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_ACTIVE_RUNS: dict[str, dict[str, Any]] = {}
_ACTIVE_RUNS_LOCK = Lock()
_SNAPSHOT_WRITER_CONDITION = Condition()
_PENDING_SNAPSHOTS: dict[str, dict[str, Any]] = {}
_SNAPSHOT_WRITER_THREAD: Thread | None = None
_SQLITE_LOCK_RETRY_DELAYS = (0.1, 0.25, 0.5, 1.0, 1.5, 2.0)
_SNAPSHOT_COALESCE_SECONDS = 0.2

logger = logging.getLogger(__name__)


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
                    previous_message = step.get("message")
                    if previous_message and previous_message != message:
                        step["previous_message"] = previous_message
                    step["message"] = message
                step["updated_at"] = timestamp

        payload = _copy_payload(run)

    try:
        _queue_run_snapshot(run_id, payload)
    except Exception:
        logger.exception("Failed to queue workflow run snapshot. run_id=%s", run_id)
    return payload


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
        active = _ACTIVE_RUNS.get(run_id)

    payload = active or serialize_run(row)
    payload["status"] = status
    payload["message"] = message
    payload["result"] = result
    payload["error"] = error
    finished_at = datetime.now(timezone.utc)
    payload["finished_at"] = finished_at.isoformat()

    row.status = status
    row.finished_at = finished_at
    row.summary_json = json.dumps(_summary_payload(payload))
    session.flush()
    _discard_pending_snapshot(run_id)
    with _ACTIVE_RUNS_LOCK:
        _ACTIVE_RUNS.pop(run_id, None)
    return payload


def finalize_run_with_retry(
    run_id: str,
    *,
    status: str,
    message: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    last_error: OperationalError | None = None
    for attempt, delay in enumerate((0.0, *_SQLITE_LOCK_RETRY_DELAYS), start=1):
        if delay:
            time.sleep(delay)
        try:
            with SessionLocal() as session:
                payload = finalize_run(
                    session,
                    run_id,
                    status=status,
                    message=message,
                    result=result,
                    error=error,
                )
                session.commit()
                return payload
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc):
                raise
            last_error = exc
            if attempt > len(_SQLITE_LOCK_RETRY_DELAYS):
                break

    payload = _finalize_active_payload(
        run_id,
        status=status,
        message=message,
        result=result,
        error=error,
    )
    if last_error is not None:
        logger.warning(
            "Could not persist final workflow run state after retries; keeping final state in memory. run_id=%s error=%s",
            run_id,
            last_error,
        )
    _queue_run_snapshot(run_id, payload)
    return payload


def _queue_run_snapshot(run_id: str, payload: dict[str, Any]) -> None:
    global _SNAPSHOT_WRITER_THREAD
    with _SNAPSHOT_WRITER_CONDITION:
        _PENDING_SNAPSHOTS[run_id] = _copy_payload(payload)
        if _SNAPSHOT_WRITER_THREAD is None or not _SNAPSHOT_WRITER_THREAD.is_alive():
            _SNAPSHOT_WRITER_THREAD = Thread(
                target=_snapshot_writer_loop,
                name="workflow-run-snapshot-writer",
                daemon=True,
            )
            _SNAPSHOT_WRITER_THREAD.start()
        _SNAPSHOT_WRITER_CONDITION.notify()


def _snapshot_writer_loop() -> None:
    while True:
        with _SNAPSHOT_WRITER_CONDITION:
            while not _PENDING_SNAPSHOTS:
                _SNAPSHOT_WRITER_CONDITION.wait()
            _SNAPSHOT_WRITER_CONDITION.wait(timeout=_SNAPSHOT_COALESCE_SECONDS)
            items = list(_PENDING_SNAPSHOTS.items())
            _PENDING_SNAPSHOTS.clear()

        for run_id, payload in items:
            if _persist_run_snapshot_best_effort(run_id, payload):
                _drop_finalized_active_run(run_id, payload)


def _persist_run_snapshot_best_effort(run_id: str, payload: dict[str, Any]) -> bool:
    for attempt, delay in enumerate((0.0, *_SQLITE_LOCK_RETRY_DELAYS), start=1):
        if delay:
            time.sleep(delay)
        try:
            with SessionLocal() as session:
                row = session.get(WorkflowRun, run_id)
                if row is None:
                    return True
                payload_status = str(payload.get("status") or "")
                if row.status in FINAL_STATUSES and payload_status not in FINAL_STATUSES:
                    return True
                row.status = payload_status or row.status
                finished_at = _parse_iso_datetime(payload.get("finished_at"))
                if finished_at is not None:
                    row.finished_at = finished_at
                row.summary_json = json.dumps(_summary_payload(payload, fallback_agent_name=row.trigger_type))
                session.commit()
                return True
        except OperationalError as exc:
            if not _is_sqlite_lock_error(exc) or attempt > len(_SQLITE_LOCK_RETRY_DELAYS):
                logger.warning("Could not persist workflow run snapshot. run_id=%s error=%s", run_id, exc)
                return False
    return False


def _discard_pending_snapshot(run_id: str) -> None:
    with _SNAPSHOT_WRITER_CONDITION:
        _PENDING_SNAPSHOTS.pop(run_id, None)


def _drop_finalized_active_run(run_id: str, payload: dict[str, Any]) -> None:
    if str(payload.get("status") or "") not in FINAL_STATUSES:
        return
    with _ACTIVE_RUNS_LOCK:
        active = _ACTIVE_RUNS.get(run_id)
        if active is not None and str(active.get("status") or "") in FINAL_STATUSES:
            _ACTIVE_RUNS.pop(run_id, None)


def _finalize_active_payload(
    run_id: str,
    *,
    status: str,
    message: str,
    result: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    with _ACTIVE_RUNS_LOCK:
        payload = _copy_payload(_ACTIVE_RUNS.get(run_id) or {"id": run_id, "steps": []})
        payload["status"] = status
        payload["message"] = message
        payload["result"] = result
        payload["error"] = error
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
        _ACTIVE_RUNS[run_id] = payload
    return payload


def _summary_payload(payload: dict[str, Any], *, fallback_agent_name: str | None = None) -> dict[str, Any]:
    return {
        "agent_name": payload.get("agent_name") or fallback_agent_name,
        "message": payload.get("message") or "",
        "steps": payload.get("steps", []),
        "result": payload.get("result"),
        "error": payload.get("error"),
        "cancel_requested": bool(payload.get("cancel_requested")),
        "run_payload": payload.get("run_payload") or {},
    }


def _copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, default=str))


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_sqlite_lock_error(exc: OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


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
