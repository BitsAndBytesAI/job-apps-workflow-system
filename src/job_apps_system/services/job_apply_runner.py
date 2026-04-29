from __future__ import annotations

import logging
from threading import Thread
from typing import Any

from sqlalchemy import select

from job_apps_system.agents.job_apply import JobApplyAgent
from job_apps_system.db.models.workflow_runs import WorkflowRun
from job_apps_system.db.session import SessionLocal, get_db_session
from job_apps_system.services.manual_runs import (
    activate_persisted_run,
    create_manual_run,
    finalize_run_with_retry,
    is_run_cancel_requested,
    update_active_run,
)
from job_apps_system.services.setup_config import load_setup_config


logger = logging.getLogger(__name__)


def start_job_apply_run(
    *,
    trigger_type: str = "manual",
    limit: int | None = 1,
    job_ids: list[str] | None = None,
    mode: str = "ai",
    existing_run_id: str | None = None,
) -> dict[str, Any]:
    payload = {"limit": limit or 1, "job_ids": job_ids or [], "mode": mode}
    with get_db_session() as session:
        if existing_run_id:
            run = activate_persisted_run(session, existing_run_id)
            if run is None:
                raise RuntimeError("Run not found.")
            project_id = run.get("project_id")
        else:
            project_id = load_setup_config(session).app.project_id
            active_run = session.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.project_id == project_id,
                    WorkflowRun.status.in_(("queued", "running")),
                    WorkflowRun.summary_json.like('%"agent_name": "job_apply"%'),
                )
            )
            if active_run is not None:
                raise RuntimeError("Apply Agent is already running. Wait for the current application to finish.")
            run = create_manual_run(
                session,
                agent_name="job_apply",
                project_id=project_id,
                trigger_type=trigger_type,
                run_payload=payload,
            )
            logger.info("Queued apply agent run. run_id=%s project_id=%s payload=%s", run["id"], project_id, payload)

    thread = Thread(target=_execute_job_apply, args=(run["id"], payload), daemon=True)
    thread.start()
    return run


def _execute_job_apply(run_id: str, payload: dict[str, Any]) -> None:
    logger.info("Starting apply agent worker. run_id=%s payload=%s", run_id, payload)
    update_active_run(run_id, status="running", message="Starting apply agent.")

    def step_reporter(*, name: str, status: str, message: str) -> None:
        overall_status = "running"
        if status == "failed":
            overall_status = "failed"
        update_active_run(
            run_id,
            status=overall_status,
            message=message,
            step_name=name,
            step_status=status,
        )

    try:
        with SessionLocal() as session:
            try:
                agent = JobApplyAgent(session)
                summary = agent.run(
                    limit=payload.get("limit") or 1,
                    job_ids=payload.get("job_ids") or None,
                    mode=payload.get("mode") or "ai",
                    step_reporter=step_reporter,
                    cancel_checker=lambda: is_run_cancel_requested(run_id),
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        final_status = "cancelled" if summary.cancelled else ("succeeded" if summary.ok else "failed")
        finalize_run_with_retry(
            run_id,
            status=final_status,
            message=summary.message,
            result=summary.model_dump(mode="json"),
            error=None if summary.ok or summary.cancelled else summary.message,
        )
        logger.info(
            "Finished apply agent worker. run_id=%s status=%s applied=%s failed=%s",
            run_id,
            final_status,
            summary.applied_count,
            summary.failed_count,
        )
    except Exception as exc:
        logger.exception("Apply agent worker crashed. run_id=%s", run_id)
        finalize_run_with_retry(
            run_id,
            status="failed",
            message=str(exc),
            error=str(exc),
        )
