from __future__ import annotations

from threading import Thread
from typing import Any

from job_apps_system.agents.job_scoring import JobScoringAgent
from job_apps_system.db.session import SessionLocal, get_db_session
from job_apps_system.services.manual_runs import create_manual_run, finalize_run, update_active_run
from job_apps_system.services.setup_config import load_setup_config


def start_job_scoring_run(
    *,
    trigger_type: str = "manual",
    limit: int | None = None,
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload = {"limit": limit, "job_ids": job_ids or []}
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        run = create_manual_run(
            session,
            agent_name="job_scoring",
            project_id=project_id,
            trigger_type=trigger_type,
        )

    thread = Thread(target=_execute_job_scoring, args=(run["id"], payload), daemon=True)
    thread.start()
    return run


def _execute_job_scoring(run_id: str, payload: dict[str, Any]) -> None:
    update_active_run(run_id, status="running", message="Starting scoring agent.")

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
            agent = JobScoringAgent(session)
            summary = agent.run(
                limit=payload.get("limit"),
                job_ids=payload.get("job_ids") or None,
                step_reporter=step_reporter,
            )
            final_status = "succeeded" if summary.ok else "failed"
            finalize_run(
                session,
                run_id,
                status=final_status,
                message=summary.message,
                result=summary.model_dump(mode="json"),
                error=None if summary.ok else summary.message,
            )
            session.commit()
    except Exception as exc:
        with SessionLocal() as session:
            finalize_run(
                session,
                run_id,
                status="failed",
                message=str(exc),
                error=str(exc),
            )
            session.commit()
