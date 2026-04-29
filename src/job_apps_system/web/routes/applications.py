from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.web.routes.jobs import _record_id, _serialize_job
from job_apps_system.services.setup_config import load_setup_config


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def applications_page(
    request: Request,
    job_id: str | None = Query(default=None),
    auto_apply: int | None = Query(default=None),
    manual_apply: int | None = Query(default=None),
):
    with get_db_session() as session:
        app_config = load_setup_config(session).app
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "active_tab": "applications",
            "page_title": "Applications",
            "page_description": "Review the current application job and let AI apply or open the posting manually.",
            "jobs_list_endpoint": "/applications/list",
            "show_application_columns": True,
            "use_card_layout": True,
            "show_find_jobs_button": False,
            "show_score_controls": False,
            "show_contact_action": False,
            "auto_score_enabled": False,
            "auto_score_pending_count": 0,
            "score_threshold": None,
            "page_run_agent": "",
            "page_run_label": "",
            "application_job_id": job_id or "",
            "application_auto_apply": bool(auto_apply),
            "application_manual_apply": bool(manual_apply),
            "default_sort_field": "score",
            "default_sort_direction": "desc",
            "anymailfinder_configured": False,
            "auto_find_contacts_enabled": app_config.auto_find_contacts_enabled,
            "gmail_configured": False,
        },
    )


@router.get("/list")
def list_application_jobs(job_id: str | None = Query(default=None)) -> dict[str, list]:
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        query = select(Job).where(Job.project_id == app_config.project_id)
        if job_id:
            row = session.get(Job, _record_id(app_config.project_id, job_id))
            if row is None:
                raise HTTPException(status_code=404, detail="Job not found.")
            rows = [row]
        else:
            rows = session.scalars(
                query.where((Job.applied.is_(True)) | (Job.application_status.is_not(None))).order_by(
                    Job.created_time.desc().nullslast(),
                    Job.id.asc(),
                )
            ).all()
        jobs = [_serialize_job(row) for row in rows]
    return {"jobs": jobs}
