from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha1
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.config.secrets import get_secret
from job_apps_system.db.models.interviews import InterviewRow
from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.anymailfinder.client import (
    AnymailfinderError,
    DecisionMakerResult,
    find_decision_maker_email,
    infer_decision_maker_category,
    pretty_decision_maker_category,
)


ANYMAILFINDER_PROVIDER = "anymailfinder"
_IGNORED_COMPANY_HOST_SUFFIXES = (
    "linkedin.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "workable.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
)


def load_contacts_by_job(
    session: Session,
    project_id: str,
    job_ids: list[str],
) -> dict[str, list[dict[str, object]]]:
    normalized_job_ids = [str(job_id).strip() for job_id in job_ids if str(job_id).strip()]
    if not normalized_job_ids:
        return {}

    rows = session.scalars(
        select(InterviewRow)
        .where(
            InterviewRow.project_id == project_id,
            InterviewRow.job_id.in_(normalized_job_ids),
            InterviewRow.provider == ANYMAILFINDER_PROVIDER,
        )
    ).all()

    grouped: dict[str, list[InterviewRow]] = defaultdict(list)
    for row in rows:
        if row.job_id:
            grouped[row.job_id].append(row)

    return {
        job_id: [serialize_contact(row) for row in _sort_contact_rows(grouped_rows)]
        for job_id, grouped_rows in grouped.items()
    }


def refresh_job_contacts(session: Session, job: Job) -> list[dict[str, object]]:
    api_key = get_secret("anymailfinder_api_key", session=session)
    if not api_key:
        raise ValueError("Add your Anymailfinder API key in Setup before finding contacts.")

    lookup_input = _lookup_input_for_job(job)
    categories = _categories_for_job(job)
    existing_rows = session.scalars(
        select(InterviewRow).where(
            InterviewRow.project_id == job.project_id,
            InterviewRow.job_id == job.id,
            InterviewRow.provider == ANYMAILFINDER_PROVIDER,
        )
    ).all()
    existing_by_id = {row.id: row for row in existing_rows}

    seen_keys: set[tuple[str, str, str]] = set()
    results: list[tuple[str, DecisionMakerResult]] = []
    for category in categories:
        response = find_decision_maker_email(
            api_key,
            domain=lookup_input["domain"],
            company_name=lookup_input["company_name"],
            decision_maker_category=category,
        )
        dedupe_key = _result_dedupe_key(response)
        if dedupe_key is None:
            continue
        compound_key = (category, *dedupe_key)
        if compound_key in seen_keys:
            continue
        seen_keys.add(compound_key)
        results.append((category, response))

    current_ids: set[str] = set()
    now = datetime.now(timezone.utc)
    upserted_rows: list[InterviewRow] = []
    for category, response in results:
        row_id = _contact_row_id(job.project_id, job.id, category, response)
        current_ids.add(row_id)
        row = existing_by_id.get(row_id)
        if row is None:
            row = InterviewRow(
                id=row_id,
                project_id=job.project_id,
                job_id=job.id,
                provider=ANYMAILFINDER_PROVIDER,
                created_date=now,
                selected=False,
            )
            session.add(row)

        row.project_id = job.project_id
        row.job_id = job.id
        row.company_name = job.company_name
        row.person_name = response.person_full_name
        row.email = response.best_email
        row.linkedin = response.person_linkedin_url
        row.position = response.person_job_title
        row.provider = ANYMAILFINDER_PROVIDER
        row.decision_maker_category = category
        row.email_status = response.email_status
        row.resume_url = job.resume_url
        row.job_description = job.job_description
        upserted_rows.append(row)

    for stale_row in existing_rows:
        if stale_row.id not in current_ids:
            session.delete(stale_row)

    session.flush()
    return [serialize_contact(row) for row in _sort_contact_rows(upserted_rows)]


def update_contact_selected(
    session: Session,
    *,
    project_id: str,
    job_id: str,
    contact_id: str,
    selected: bool,
) -> dict[str, object]:
    row = session.scalar(
        select(InterviewRow).where(
            InterviewRow.id == contact_id,
            InterviewRow.project_id == project_id,
            InterviewRow.job_id == job_id,
            InterviewRow.provider == ANYMAILFINDER_PROVIDER,
        )
    )
    if row is None:
        raise LookupError("Contact not found.")

    row.selected = bool(selected)
    session.flush()
    return serialize_contact(row)


def serialize_contact(row: InterviewRow) -> dict[str, object]:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "person_name": row.person_name,
        "email": row.email,
        "linkedin": row.linkedin,
        "position": row.position,
        "provider": row.provider,
        "decision_maker_category": row.decision_maker_category,
        "decision_maker_category_label": pretty_decision_maker_category(row.decision_maker_category),
        "email_status": row.email_status,
        "selected": bool(row.selected),
    }


def _lookup_input_for_job(job: Job) -> dict[str, str | None]:
    company_name = (job.company_name or "").strip() or None
    domain = _resolve_company_domain(job)
    if domain:
        return {"domain": domain, "company_name": company_name}
    if company_name:
        return {"domain": None, "company_name": company_name}
    raise ValueError("This job does not have a company name or domain Anymailfinder can use.")


def _resolve_company_domain(job: Job) -> str | None:
    for candidate in (job.company_url, job.apply_url, job.job_posting_url):
        domain = _extract_company_domain(candidate)
        if domain:
            return domain
    return None


def _extract_company_domain(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").strip().lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname or "." not in hostname:
        return None
    if any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in _IGNORED_COMPANY_HOST_SUFFIXES):
        return None
    return hostname


def _categories_for_job(job: Job) -> list[str]:
    inferred = infer_decision_maker_category(job.job_title, job.job_description) or "operations"
    categories = [category for category in (inferred, "ceo", "hr") if category]
    deduped: list[str] = []
    seen: set[str] = set()
    for category in categories:
        if category in seen:
            continue
        seen.add(category)
        deduped.append(category)
    return deduped


def _contact_row_id(project_id: str, job_id: str, category: str, response: DecisionMakerResult) -> str:
    fingerprint = "|".join(
        [
            project_id,
            job_id,
            category,
            (response.best_email or "").lower(),
            (response.person_full_name or "").strip().lower(),
            (response.person_job_title or "").strip().lower(),
        ]
    )
    digest = sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"{project_id}:{job_id}:{ANYMAILFINDER_PROVIDER}:{category}:{digest}"


def _result_dedupe_key(response: DecisionMakerResult) -> tuple[str, str] | None:
    email = (response.best_email or "").strip().lower()
    if email:
        return (email, (response.person_full_name or "").strip().lower())
    return None


def _sort_contact_rows(rows: list[InterviewRow]) -> list[InterviewRow]:
    return sorted(
        rows,
        key=lambda row: (
            _contact_priority(row.decision_maker_category),
            (row.person_name or "").lower(),
            (row.email or "").lower(),
        ),
    )


def _contact_priority(category: str | None) -> int:
    normalized = (category or "").strip().lower()
    if not normalized:
        return 99
    if normalized == "ceo":
        return 1
    if normalized == "hr":
        return 2
    return 0
