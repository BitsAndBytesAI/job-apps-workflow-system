from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from job_apps_system.agents.outreach_generation import OutreachGenerationAgent
from job_apps_system.agents.outreach_sending import send_outreach_emails
from job_apps_system.db.models.interviews import InterviewRow
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.integrations.anymailfinder.client import AnymailfinderError
from job_apps_system.schemas.jobs import (
    AutoFindContactsUpdateRequest,
    ContactSelectionUpdateRequest,
    OutreachPreviewRequest,
    OutreachSendRequest,
)
from job_apps_system.services.interview_contacts import (
    ANYMAILFINDER_PROVIDER,
    load_contacts_by_job,
    refresh_job_contacts,
    update_contact_selected,
)
from job_apps_system.services.setup_config import build_setup_update, load_setup_config, save_setup_config
from job_apps_system.web.routes.jobs import _serialize_job
from job_apps_system.web.templating import templates


router = APIRouter()


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
            "auto_generate_resumes_enabled": False,
            "auto_generate_resumes_pending_count": 0,
            "score_threshold": None,
            "page_run_agent": "",
            "page_run_label": "Contact Finder",
            "default_sort_field": "created_time",
            "default_sort_direction": "desc",
            "application_job_id": "",
            "application_auto_apply": False,
            "application_manual_apply": False,
            "anymailfinder_configured": config.secrets.anymailfinder_api_key_configured,
            "auto_find_contacts_enabled": config.app.auto_find_contacts_enabled,
            "gmail_configured": config.secrets.google_oauth_token_json.configured,
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


@router.put("/auto-find-contacts")
def update_auto_find_contacts(payload: AutoFindContactsUpdateRequest) -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        update.app.auto_find_contacts_enabled = bool(payload.enabled)
        saved = save_setup_config(session, update)
        return {"ok": True, "auto_find_contacts_enabled": saved.app.auto_find_contacts_enabled}


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


@router.post("/{job_id}/contacts/email/preview")
def preview_outreach_email(job_id: str, payload: OutreachPreviewRequest) -> dict[str, object]:
    with get_db_session() as session:
        config = load_setup_config(session)
        templates_cfg = config.email_templates
        if payload.mode == "manual":
            return {
                "ok": True,
                "mode": "manual",
                "subject": templates_cfg.last_subject,
                "body": templates_cfg.last_body,
                "bcc_self": templates_cfg.bcc_self,
            }

        if not config.secrets.anthropic_api_key_configured:
            raise HTTPException(
                status_code=400,
                detail="Add your Anthropic API key in Setup before generating outreach with AI.",
            )
        job = session.get(Job, _record_id(config.app.project_id, job_id))
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        try:
            generated = OutreachGenerationAgent(session).generate_for_job(job)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AI generation failed: {exc}") from exc

    return {
        "ok": True,
        "mode": "ai",
        "subject": generated["subject"],
        "body": generated["body"],
        "bcc_self": templates_cfg.bcc_self,
    }


@router.post("/{job_id}/contacts/email/send")
def send_outreach_email(job_id: str, payload: OutreachSendRequest) -> dict[str, object]:
    with get_db_session() as session:
        config = load_setup_config(session)
        if not config.secrets.google_oauth_token_json.configured:
            raise HTTPException(
                status_code=400,
                detail="Connect Google in Setup before sending email.",
            )
        job = session.get(Job, _record_id(config.app.project_id, job_id))
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        try:
            results = send_outreach_emails(
                session,
                job=job,
                contact_ids=payload.contact_ids,
                subject=payload.subject,
                body=payload.body,
                bcc_self=payload.bcc_self,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        update = build_setup_update(config)
        update.email_templates.last_subject = payload.subject
        update.email_templates.last_body = payload.body
        update.email_templates.bcc_self = payload.bcc_self
        save_setup_config(session, update)

    sent_count = sum(1 for r in results if r.get("ok"))
    failed_count = len(results) - sent_count
    return {
        "ok": True,
        "results": results,
        "sent_count": sent_count,
        "failed_count": failed_count,
    }


@router.get("/{job_id}/contacts/{contact_id}/email")
def get_sent_email(job_id: str, contact_id: str) -> dict[str, object]:
    with get_db_session() as session:
        project_id = load_setup_config(session).app.project_id
        row = session.scalar(
            select(InterviewRow).where(
                InterviewRow.id == contact_id,
                InterviewRow.job_id == job_id,
                InterviewRow.project_id == project_id,
                InterviewRow.provider == ANYMAILFINDER_PROVIDER,
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Contact not found.")
        if not row.email_sent:
            raise HTTPException(status_code=404, detail="No email has been sent to this contact.")
        return {
            "ok": True,
            "contact_id": row.id,
            "to": row.email,
            "person_name": row.person_name,
            "subject": row.email_subject,
            "body": row.email_contents,
            "bcc": row.email_bcc,
            "sent_at": row.email_sent_at.isoformat() if row.email_sent_at else None,
        }


def _record_id(project_id: str, job_id: str) -> str:
    return f"{project_id}:{job_id}"
