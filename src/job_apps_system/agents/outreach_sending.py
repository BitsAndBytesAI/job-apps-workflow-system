from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.interviews import InterviewRow
from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.google.gmail import send_email
from job_apps_system.services.interview_contacts import (
    ANYMAILFINDER_PROVIDER,
    serialize_contact,
)
from job_apps_system.services.setup_config import load_setup_config


_PLACEHOLDER_PATTERNS: list[tuple[str, str]] = [
    ("name", "person_name"),
    ("contact name", "person_name"),
    ("contact_name", "person_name"),
    ("title", "position"),
    ("contact title", "position"),
    ("contact_title", "position"),
    ("position", "position"),
]


def send_outreach_emails(
    session: Session,
    *,
    job: Job,
    contact_ids: list[str],
    subject: str,
    body: str,
    bcc_self: bool,
) -> list[dict[str, object]]:
    if not subject.strip():
        raise ValueError("Subject is required.")
    if not body.strip():
        raise ValueError("Body is required.")
    if not contact_ids:
        raise ValueError("Select at least one contact to email.")

    config = load_setup_config(session)
    applicant = config.applicant
    sender_email = (applicant.email or "").strip()
    bcc_address = sender_email if bcc_self and sender_email else None

    resume_link = _resolve_resume_link(job, config.project_resume.source_url)
    body_with_resume = _append_resume_link(body, resume_link)

    rows = session.scalars(
        select(InterviewRow).where(
            InterviewRow.id.in_(contact_ids),
            InterviewRow.job_id == job.id,
            InterviewRow.project_id == job.project_id,
            InterviewRow.provider == ANYMAILFINDER_PROVIDER,
        )
    ).all()
    rows_by_id = {row.id: row for row in rows}

    results: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    for contact_id in contact_ids:
        row = rows_by_id.get(contact_id)
        if row is None:
            results.append({
                "contact_id": contact_id,
                "ok": False,
                "error": "Contact not found.",
            })
            continue
        if not (row.email or "").strip():
            results.append({
                "contact_id": contact_id,
                "ok": False,
                "error": "Contact has no email address.",
            })
            continue

        personalized_subject = _substitute_placeholders(subject, row, applicant)
        personalized_body = _substitute_placeholders(body_with_resume, row, applicant)

        try:
            send_email(
                to=row.email,
                subject=personalized_subject,
                body=personalized_body,
                bcc=bcc_address,
                session=session,
            )
        except Exception as exc:
            results.append({
                "contact_id": contact_id,
                "ok": False,
                "error": str(exc),
            })
            continue

        row.email_subject = personalized_subject
        row.email_contents = personalized_body
        row.email_bcc = bcc_address
        row.email_sent = True
        row.email_sent_at = now
        row.selected = False
        session.flush()
        results.append({
            "contact_id": contact_id,
            "ok": True,
            "contact": serialize_contact(row),
        })

    return results


def _resolve_resume_link(job: Job, fallback_source_url: str | None) -> str | None:
    candidate = (job.resume_url or "").strip()
    if candidate:
        return candidate
    fallback = (fallback_source_url or "").strip()
    return fallback or None


def _append_resume_link(body: str, resume_link: str | None) -> str:
    if not resume_link:
        return body
    suffix = f"\n\nResume: {resume_link}"
    return body.rstrip() + suffix


def _substitute_placeholders(
    template: str,
    row: InterviewRow,
    applicant: ApplicantProfileConfig,
) -> str:
    values = {
        "person_name": (row.person_name or "there").strip() or "there",
        "position": (row.position or "your team").strip() or "your team",
    }
    text = template
    for token, field in _PLACEHOLDER_PATTERNS:
        replacement = values[field]
        # `<token>` form (with optional whitespace around the token)
        text = re.sub(rf"<\s*{re.escape(token)}\s*>", replacement, text, flags=re.IGNORECASE)
        # `{token}` form
        text = re.sub(rf"\{{\s*{re.escape(token)}\s*\}}", replacement, text, flags=re.IGNORECASE)
    return text


# Class wrapper for symmetry with other agents in this codebase.
class OutreachSendingAgent:
    def __init__(self, session: Session) -> None:
        self._session = session

    def send(
        self,
        *,
        job: Job,
        contact_ids: list[str],
        subject: str,
        body: str,
        bcc_self: bool,
    ) -> list[dict[str, object]]:
        return send_outreach_emails(
            self._session,
            job=job,
            contact_ids=contact_ids,
            subject=subject,
            body=body,
            bcc_self=bcc_self,
        )
