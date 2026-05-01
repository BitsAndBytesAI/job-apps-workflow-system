from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import func, or_, select

from job_apps_system.config.settings import settings
from job_apps_system.agents.job_intake import JobIntakeAgent
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.schemas.jobs import (
    AutoGenerateResumesUpdateRequest,
    AutoScoreUpdateRequest,
    HideJobCardRequest,
    JobIntakeRunRequest,
    JobUpdateRequest,
    MoveToApplicationsRequest,
    ScoreThresholdUpdateRequest,
)
from job_apps_system.services.job_intake_runner import start_job_intake_run
from job_apps_system.services.setup_config import build_setup_update, load_setup_config, save_setup_config
from job_apps_system.web.templating import templates


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def jobs_page(request: Request):
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        pending_scoring_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.project_id == app_config.project_id,
                or_(Job.intake_decision.is_(None), Job.intake_decision == "accepted"),
                Job.score.is_(None),
                Job.job_description.is_not(None),
                Job.job_description != "",
            )
        ) or 0
        pending_resume_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.project_id == app_config.project_id,
                or_(Job.intake_decision.is_(None), Job.intake_decision == "accepted"),
                Job.score.is_not(None),
                Job.score >= app_config.score_threshold,
                or_(Job.resume_url.is_(None), Job.resume_url == ""),
                or_(Job.applied.is_(False), Job.applied.is_(None)),
                or_(Job.application_status.is_(None), Job.application_status == ""),
                or_(Job.hidden_from_best_matches.is_(False), Job.hidden_from_best_matches.is_(None)),
            )
        ) or 0
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "page_title": "Best Job Matches",
            "page_description": "Review scored jobs that clear the threshold, generate targeted resumes, and apply.",
            "jobs_list_endpoint": "/jobs/list",
            "active_tab": "best_job_matches",
            "show_application_columns": True,
            "use_card_layout": True,
            "show_find_jobs_button": False,
            "show_score_controls": True,
            "show_contact_action": False,
            "auto_score_enabled": app_config.auto_score_enabled,
            "auto_score_pending_count": pending_scoring_count,
            "auto_generate_resumes_enabled": app_config.auto_generate_resumes_enabled,
            "auto_generate_resumes_pending_count": pending_resume_count,
            "score_threshold": app_config.score_threshold,
            "page_run_agent": "job_scoring",
            "page_run_label": "Scoring Agent",
            "default_sort_field": "score",
            "default_sort_direction": "desc",
            "anymailfinder_configured": False,
            "auto_find_contacts_enabled": False,
            "gmail_configured": False,
        },
    )


@router.get("/all/", response_class=HTMLResponse)
def all_jobs_page(request: Request):
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "page_title": "Find Jobs",
            "page_description": "Browse every scraped job in the local database and refresh the list by running the Jobs Agent.",
            "jobs_list_endpoint": "/jobs/all/list",
            "active_tab": "find_jobs",
            "show_application_columns": False,
            "use_card_layout": False,
            "show_find_jobs_button": True,
            "show_score_controls": False,
            "show_contact_action": False,
            "auto_score_enabled": False,
            "auto_score_pending_count": 0,
            "auto_generate_resumes_enabled": False,
            "auto_generate_resumes_pending_count": 0,
            "score_threshold": None,
            "page_run_agent": "job_intake",
            "page_run_label": "Jobs Agent",
            "default_sort_field": "created_time",
            "default_sort_direction": "desc",
            "anymailfinder_configured": False,
            "auto_find_contacts_enabled": False,
            "gmail_configured": False,
        },
    )


@router.get("/list")
def list_jobs(threshold: int | None = Query(default=None, ge=0, le=1000)) -> dict[str, list]:
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        effective_threshold = threshold if threshold is not None else app_config.score_threshold
        query = (
            select(Job)
            .where(Job.project_id == app_config.project_id)
            .where(or_(Job.intake_decision.is_(None), Job.intake_decision == "accepted"))
            .where(Job.score.is_not(None), Job.score >= effective_threshold)
            .where(or_(Job.applied.is_(False), Job.applied.is_(None)))
            .where(or_(Job.application_status.is_(None), Job.application_status == ""))
            .where(or_(Job.hidden_from_best_matches.is_(False), Job.hidden_from_best_matches.is_(None)))
        )
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
        # When applied transitions to true (e.g. via the manual apply Yes
        # confirmation), stamp applied_at the same way the AI apply agent
        # does on success. Without this, manual applies have no Applied-on
        # date for the Applications page badge to display.
        if updates.get("applied") is True and row.applied_at is None:
            row.applied_at = datetime.now(timezone.utc)
        session.flush()
        return {"ok": True, "job": _serialize_job(row)}


@router.post("/{job_id}/move-to-applications")
def move_job_to_applications(job_id: str, payload: MoveToApplicationsRequest) -> dict:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        row = session.get(Job, _record_id(project_id, job_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        if not row.application_status:
            row.application_status = "apply_started"

        session.flush()
        return {"ok": True, "job": _serialize_job(row)}


@router.post("/{job_id}/hide-card")
def hide_job_card(job_id: str, payload: HideJobCardRequest) -> dict:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        row = session.get(Job, _record_id(project_id, job_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        if payload.page == "best_matches":
            row.hidden_from_best_matches = True
        elif payload.page == "applications":
            row.hidden_from_applications = True
        else:
            raise HTTPException(status_code=400, detail="Unsupported card page.")

        session.flush()
        return {"ok": True, "job": _serialize_job(row)}


@router.put("/score-threshold")
def update_score_threshold(payload: ScoreThresholdUpdateRequest) -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        update.app.score_threshold = payload.score_threshold
        saved = save_setup_config(session, update)
        return {"ok": True, "score_threshold": saved.app.score_threshold}


@router.put("/auto-score")
def update_auto_score(payload: AutoScoreUpdateRequest) -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        update.app.auto_score_enabled = bool(payload.enabled)
        saved = save_setup_config(session, update)
        return {"ok": True, "auto_score_enabled": saved.app.auto_score_enabled}


@router.put("/auto-generate-resumes")
def update_auto_generate_resumes(payload: AutoGenerateResumesUpdateRequest) -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        update.app.auto_generate_resumes_enabled = bool(payload.enabled)
        saved = save_setup_config(session, update)
        return {"ok": True, "auto_generate_resumes_enabled": saved.app.auto_generate_resumes_enabled}


@router.get("/{job_id}/application-screenshot")
def get_application_screenshot(job_id: str):
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        row = session.get(Job, _record_id(project_id, job_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        screenshot_path_value = row.application_screenshot_path
        if not screenshot_path_value:
            raise HTTPException(status_code=404, detail="Application screenshot not found.")

    screenshot_path = Path(screenshot_path_value).expanduser().resolve()
    app_data_dir = settings.resolved_app_data_dir.resolve()
    try:
        screenshot_path.relative_to(app_data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Application screenshot path is outside app data.") from exc
    if not screenshot_path.is_file():
        raise HTTPException(status_code=404, detail="Application screenshot file does not exist.")

    return FileResponse(screenshot_path)


@router.post("/intake/run")
def run_job_intake(payload: JobIntakeRunRequest) -> dict:
    with get_db_session() as session:
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

    return summary.model_dump(mode="json")


@router.post("/intake/start")
def start_job_intake(payload: JobIntakeRunRequest) -> dict:
    return start_job_intake_run(
        trigger_type="manual",
        trigger_source=payload.trigger_source or "api_jobs_intake_start",
        search_urls=payload.search_urls or None,
        max_jobs_per_search=payload.max_jobs_per_search,
    )


def _record_id(project_id: str, job_id: str) -> str:
    return f"{project_id}:{job_id}"


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
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "resume_url": row.resume_url,
        "apply_url": row.apply_url,
        "company_url": row.company_url,
        "company_domain": row.company_domain,
        "application_status": row.application_status,
        "application_error": row.application_error,
        "application_screenshot_path": row.application_screenshot_path,
        "application_screenshot_url": f"/jobs/{row.id}/application-screenshot" if row.application_screenshot_path else None,
        "hidden_from_best_matches": bool(row.hidden_from_best_matches),
        "hidden_from_applications": bool(row.hidden_from_applications),
        "created_time": row.created_time.isoformat() if row.created_time else None,
    }
