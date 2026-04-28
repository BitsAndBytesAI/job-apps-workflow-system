from __future__ import annotations

from fastapi import APIRouter, HTTPException

from job_apps_system.agents.resume_generation import ResumeGenerationAgent
from job_apps_system.db.session import get_db_session
from job_apps_system.schemas.resumes import ResumeGenerationRunRequest
from job_apps_system.services.resume_generation_runner import start_resume_generation_run


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
def start_resume_generation(payload: ResumeGenerationRunRequest) -> dict:
    return start_resume_generation_run(
        trigger_type="manual",
        limit=payload.limit,
        job_ids=payload.job_ids or None,
    )
