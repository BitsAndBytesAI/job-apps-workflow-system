from __future__ import annotations

from fastapi import APIRouter, HTTPException

from job_apps_system.agents.job_apply import JobApplyAgent
from job_apps_system.db.session import get_db_session
from job_apps_system.schemas.apply import ApplyRunRequest
from job_apps_system.services.job_apply_runner import start_job_apply_run


router = APIRouter()


@router.post("/run")
def run_apply(payload: ApplyRunRequest) -> dict:
    with get_db_session() as session:
        agent = JobApplyAgent(session)
        try:
            summary = agent.run(limit=payload.limit or 1, job_ids=payload.job_ids or None)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not summary.ok:
        raise HTTPException(status_code=400, detail=summary.message)

    return summary.model_dump(mode="json")


@router.post("/start")
def start_apply(payload: ApplyRunRequest) -> dict:
    job_ids = payload.job_ids or []
    if len(job_ids) != 1:
        raise HTTPException(status_code=400, detail="Apply Agent currently runs for exactly one selected job.")
    try:
        return start_job_apply_run(limit=1, job_ids=job_ids, trigger_type="manual")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
