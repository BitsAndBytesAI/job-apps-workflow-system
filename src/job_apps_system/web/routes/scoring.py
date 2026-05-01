from __future__ import annotations

from fastapi import APIRouter, HTTPException

from job_apps_system.agents.job_scoring import JobScoringAgent
from job_apps_system.db.session import get_db_session
from job_apps_system.schemas.scoring import JobScoringRunRequest
from job_apps_system.services.job_scoring_runner import start_job_scoring_run


router = APIRouter()


@router.post("/run")
def run_job_scoring(payload: JobScoringRunRequest) -> dict:
    with get_db_session() as session:
        agent = JobScoringAgent(session)
        try:
            summary = agent.run(limit=payload.limit, job_ids=payload.job_ids or None)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not summary.ok:
        raise HTTPException(status_code=400, detail=summary.message)

    return summary.model_dump(mode="json")


@router.post("/start")
def start_scoring(payload: JobScoringRunRequest) -> dict:
    return start_job_scoring_run(
        limit=payload.limit,
        job_ids=payload.job_ids or None,
        trigger_type="manual",
        trigger_source=payload.trigger_source or "api_scoring_start",
    )
