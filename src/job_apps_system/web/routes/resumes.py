from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException

from job_apps_system.agents.resume_generation import ResumeGenerationAgent
from job_apps_system.db.session import SessionLocal, get_db_session
from job_apps_system.schemas.resumes import ResumeGenerationRunRequest
from job_apps_system.services.manual_runs import (
    create_manual_run,
    finalize_run,
    is_run_cancel_requested,
    update_active_run,
)
from job_apps_system.services.setup_config import load_setup_config


router = APIRouter()


@router.post("/generate/run")
def run_resume_generation(payload: ResumeGenerationRunRequest) -> dict:
    with get_db_session() as session:
        agent = ResumeGenerationAgent(session)
        try:
            summary = agent.run(limit=payload.limit, job_ids=payload.job_ids)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not summary.ok:
        raise HTTPException(status_code=400, detail=summary.message)

    return summary.model_dump(mode="json")


@router.post("/generate/start")
def start_resume_generation(payload: ResumeGenerationRunRequest, background_tasks: BackgroundTasks) -> dict:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        run = create_manual_run(
            session,
            agent_name="resume_generation",
            project_id=project_id,
            trigger_type="manual",
        )

    background_tasks.add_task(_execute_resume_generation, run["id"], payload.model_dump(mode="json"))
    return run


def _execute_resume_generation(run_id: str, payload: dict) -> None:
    update_active_run(run_id, status="running", message="Starting resume agent.")

    def step_reporter(name: str, status: str, message: str) -> None:
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
            agent = ResumeGenerationAgent(session)
            summary = agent.run(
                limit=payload.get("limit"),
                job_ids=payload.get("job_ids") or None,
                step_reporter=step_reporter,
                cancel_checker=lambda: is_run_cancel_requested(run_id),
            )
            final_status = "cancelled" if summary.cancelled else ("succeeded" if summary.ok else "failed")
            finalize_run(
                session,
                run_id,
                status=final_status,
                message=summary.message,
                result=summary.model_dump(mode="json"),
                error=None if summary.ok or summary.cancelled else summary.message,
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
