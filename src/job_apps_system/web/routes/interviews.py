from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.integrations.anymailfinder.client import AnymailfinderError
from job_apps_system.schemas.jobs import ContactSelectionUpdateRequest
from job_apps_system.services.interview_contacts import (
    load_contacts_by_job,
    refresh_job_contacts,
    update_contact_selected,
)
from job_apps_system.services.setup_config import load_setup_config
from job_apps_system.web.routes.jobs import _serialize_job


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def interviews_page(request: Request):
    with get_db_session() as session:
        config = load_setup_config(session)
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
            "anymailfinder_configured": config.secrets.anymailfinder_api_key_configured,
        },
    )


@router.get("/list")
def list_emails_interviews_jobs() -> dict[str, list]:
    with get_db_session() as session:
        config = load_setup_config(session)
        app_config = config.app
        rows = session.scalars(
            select(Job)
            .where(Job.project_id == app_config.project_id, Job.applied.is_(True))
            .order_by(Job.applied_at.desc().nullslast(), Job.created_time.desc().nullslast(), Job.id.asc())
        ).all()
        contacts_by_job = load_contacts_by_job(session, app_config.project_id, [row.id for row in rows])
        jobs = [
            {
                **_serialize_job(row),
                "contacts": contacts_by_job.get(row.id, []),
            }
            for row in rows
        ]
    return {"jobs": jobs}


@router.post("/{job_id}/contacts/find")
def find_interview_contacts(job_id: str) -> dict[str, object]:
    with get_db_session() as session:
        config = load_setup_config(session)
        if not config.secrets.anymailfinder_api_key_configured:
            raise HTTPException(
                status_code=400,
                detail="Add your Anymailfinder API key in Setup before finding contacts.",
            )

        row = session.get(Job, _record_id(config.app.project_id, job_id))
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        try:
            contacts = refresh_job_contacts(session, row)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AnymailfinderError as exc:
            status_code = 400 if exc.status_code in {400, 401, 402} else 502
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    return {"ok": True, "contacts": contacts}


@router.patch("/{job_id}/contacts/{contact_id}")
def update_interview_contact_selection(job_id: str, contact_id: str, payload: ContactSelectionUpdateRequest) -> dict[str, object]:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        try:
            contact = update_contact_selected(
                session,
                project_id=project_id,
                job_id=job_id,
                contact_id=contact_id,
                selected=payload.selected,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "contact": contact}


def _record_id(project_id: str, job_id: str) -> str:
    return f"{project_id}:{job_id}"
