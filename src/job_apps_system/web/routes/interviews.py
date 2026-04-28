from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.services.setup_config import load_setup_config
from job_apps_system.web.routes.jobs import _serialize_job


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def interviews_page(request: Request):
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "active_tab": "emails_interviews",
            "page_title": "Emails/Interviews",
            "page_description": "Find Job Contacts, send emails, track interviews, send Thank you notes",
            "jobs_list_endpoint": "/interviews/list",
            "show_application_columns": True,
            "use_card_layout": True,
            "show_find_jobs_button": False,
            "show_score_controls": False,
            "show_contact_action": True,
            "auto_score_enabled": False,
            "auto_score_pending_count": 0,
            "score_threshold": None,
            "page_run_agent": "",
            "page_run_label": "",
            "default_sort_field": "created_time",
            "default_sort_direction": "desc",
            "application_job_id": "",
            "application_auto_apply": False,
            "application_manual_apply": False,
        },
    )


@router.get("/list")
def list_emails_interviews_jobs() -> dict[str, list]:
    with get_db_session() as session:
        app_config = load_setup_config(session).app
        rows = session.scalars(
            select(Job)
            .where(Job.project_id == app_config.project_id, Job.applied.is_(True))
            .order_by(Job.applied_at.desc().nullslast(), Job.created_time.desc().nullslast(), Job.id.asc())
        ).all()
        jobs = [_serialize_job(row) for row in rows]
    return {"jobs": jobs}
