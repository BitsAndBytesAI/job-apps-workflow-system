from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select

from job_apps_system.agents.job_intake import JobIntakeAgent
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import SessionLocal, get_db_session
from job_apps_system.schemas.jobs import JobIntakeRunRequest, JobUpdateRequest
from job_apps_system.services.job_scoring_runner import start_job_scoring_run
from job_apps_system.services.manual_runs import (
    create_manual_run,
    finalize_run,
    is_run_cancel_requested,
    update_active_run,
)
from job_apps_system.services.setup_config import load_setup_config


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def jobs_page(request: Request):
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "page_title": "Applications",
            "page_description": "Browse, search, and edit tracked application rows that are not duplicates.",
            "jobs_list_endpoint": "/jobs/list",
            "active_nav": "applications",
            "show_application_columns": True,
            "use_card_layout": True,
        },
    )


@router.get("/all/", response_class=HTMLResponse)
def all_jobs_page(request: Request):
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "page_title": "All Jobs",
            "page_description": "Browse, search, and inspect every scraped job row in the local database.",
            "jobs_list_endpoint": "/jobs/all/list",
            "active_nav": "all_jobs",
            "show_application_columns": False,
            "use_card_layout": False,
        },
    )


@router.get("/list")
def list_jobs() -> dict[str, list]:
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        query = (
            select(Job)
            .where(Job.project_id == app_config.project_id)
            .where(or_(Job.intake_decision.is_(None), Job.intake_decision == "accepted"))
        )
        if app_config.hide_jobs_below_score_threshold:
            query = query.where(Job.score.is_not(None), Job.score >= app_config.score_threshold)
        rows = session.scalars(
            query.order_by(Job.created_time.desc().nullslast(), Job.id.asc())
        ).all()
        jobs = [_serialize_job(row) for row in rows]
    return {"jobs": jobs}


@router.get("/all/list")
def list_all_jobs() -> dict[str, list]:
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        query = select(Job).where(Job.project_id == app_config.project_id)
        rows = session.scalars(
            query.order_by(Job.created_time.desc().nullslast(), Job.id.asc())
        ).all()
        jobs = [_serialize_job(row) for row in rows]
    return {"jobs": jobs}


@router.patch("/{job_id}")
def update_job(job_id: str, payload: JobUpdateRequest) -> dict:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields were provided.")

    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        row = session.get(Job, _record_id(project_id, job_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        for field_name, value in updates.items():
            setattr(row, field_name, value)
        session.flush()
        return {"ok": True, "job": _serialize_job(row)}


@router.post("/intake/run")
def run_job_intake(payload: JobIntakeRunRequest) -> dict:
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        dry_run = app_config.dry_run
        agent = JobIntakeAgent(session)
        try:
            summary = agent.run(
                search_urls=payload.search_urls or None,
                max_jobs_per_search=payload.max_jobs_per_search,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not summary.ok:
        raise HTTPException(status_code=400, detail=summary.message)

    if summary.accepted_jobs and not dry_run:
        start_job_scoring_run(
            trigger_type="job_intake",
            job_ids=[job.id for job in summary.accepted_jobs],
        )

    return summary.model_dump(mode="json")


@router.post("/intake/start")
def start_job_intake(payload: JobIntakeRunRequest, background_tasks: BackgroundTasks) -> dict:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        run = create_manual_run(session, agent_name="job_intake", project_id=project_id, trigger_type="manual")

    background_tasks.add_task(_execute_job_intake, run["id"], payload.model_dump(mode="json"))
    return run


def _execute_job_intake(run_id: str, payload: dict) -> None:
    update_active_run(run_id, status="running", message="Starting jobs agent.")
    scoring_job_ids: list[str] = []

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
            app_config = load_setup_config(session).app
            dry_run = app_config.dry_run
            agent = JobIntakeAgent(session)
            summary = agent.run(
                search_urls=payload.get("search_urls") or None,
                max_jobs_per_search=_optional_positive_int(payload.get("max_jobs_per_search")),
                step_reporter=step_reporter,
                cancel_checker=lambda: is_run_cancel_requested(run_id),
            )
            if summary.ok and summary.accepted_jobs and not dry_run:
                scoring_job_ids = [job.id for job in summary.accepted_jobs]
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
        if scoring_job_ids:
            start_job_scoring_run(
                trigger_type="job_intake",
                job_ids=scoring_job_ids,
            )
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


def _record_id(project_id: str, job_id: str) -> str:
    return f"{project_id}:{job_id}"


def _optional_positive_int(value) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _serialize_job(row: Job) -> dict[str, object]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "tracking_id": row.tracking_id,
        "company_name": row.company_name,
        "job_title": row.job_title,
        "job_description": row.job_description,
        "posted_date": row.posted_date,
        "job_posting_url": row.job_posting_url,
        "score": row.score,
        "applied": row.applied,
        "resume_url": row.resume_url,
        "apply_url": row.apply_url,
        "company_url": row.company_url,
        "created_time": row.created_time.isoformat() if row.created_time else None,
    }
