from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select

from job_apps_system.db.models.emails import EmailDelivery
from job_apps_system.db.models.interviews import InterviewRow
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.services.manual_runs import list_runs as list_run_records
from job_apps_system.services.setup_config import load_setup_config
from job_apps_system.web.templating import templates


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_db_session() as session:
        config = load_setup_config(session)
        project_id = config.app.project_id
        score_threshold = config.app.score_threshold

        total_jobs = session.scalar(
            select(func.count()).select_from(Job).where(Job.project_id == project_id)
        ) or 0
        best_match_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.project_id == project_id,
                or_(Job.intake_decision.is_(None), Job.intake_decision == "accepted"),
                Job.score.is_not(None),
                Job.score >= score_threshold,
            )
        ) or 0
        resume_ready_count = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(
                Job.project_id == project_id,
                or_(Job.intake_decision.is_(None), Job.intake_decision == "accepted"),
                Job.resume_url.is_not(None),
                Job.resume_url != "",
            )
        ) or 0
        applied_count = session.scalar(
            select(func.count()).select_from(Job).where(Job.project_id == project_id, Job.applied.is_(True))
        ) or 0
        emailed_job_count = session.scalar(
            select(func.count(func.distinct(EmailDelivery.job_id)))
            .select_from(EmailDelivery)
            .join(Job, Job.id == EmailDelivery.job_id)
            .where(
                Job.project_id == project_id,
                EmailDelivery.job_id.is_not(None),
                or_(EmailDelivery.sent_at.is_not(None), EmailDelivery.status == "sent"),
            )
        ) or 0
        latest_email_sent = session.scalar(
            select(func.max(EmailDelivery.sent_at))
            .select_from(EmailDelivery)
            .join(Job, Job.id == EmailDelivery.job_id)
            .where(
                Job.project_id == project_id,
                EmailDelivery.job_id.is_not(None),
                or_(EmailDelivery.sent_at.is_not(None), EmailDelivery.status == "sent"),
            )
        )
        # Job Emails pill counts the email contacts we've actually
        # discovered — InterviewRow rows from the Anymailfinder lookup
        # WITH a resolved email address. Searches that yielded "not found"
        # placeholder rows (e.g. "Engineering contact not found") are
        # excluded so the count reflects real contacts the user can email.
        email_contact_count = session.scalar(
            select(func.count(func.distinct(InterviewRow.id)))
            .select_from(InterviewRow)
            .join(Job, Job.id == InterviewRow.job_id)
            .where(
                Job.project_id == project_id,
                InterviewRow.job_id.is_not(None),
                InterviewRow.provider == "anymailfinder",
                InterviewRow.email.is_not(None),
                InterviewRow.email != "",
            )
        ) or 0
        # Interviews pill only counts manually-added interview rows
        # (provider null or anything other than the contact-lookup provider).
        # Updates when the user manually adds an interview entry, not when
        # a contact is discovered.
        interview_count = session.scalar(
            select(func.count(func.distinct(InterviewRow.id)))
            .select_from(InterviewRow)
            .join(Job, Job.id == InterviewRow.job_id)
            .where(
                Job.project_id == project_id,
                InterviewRow.job_id.is_not(None),
                or_(InterviewRow.provider.is_(None), InterviewRow.provider != "anymailfinder"),
            )
        ) or 0

        runs = list_run_records(session, project_id=project_id)

    latest_intake = _latest_run(runs, "job_intake")
    latest_scoring = _latest_run(runs, "job_scoring")
    latest_resume = _latest_run(runs, "resume_generation")
    latest_apply = _latest_run(runs, "job_apply")

    overview_stats = [
        {"label": "Total Jobs", "value": str(total_jobs)},
        {"label": "Best Matches", "value": str(best_match_count)},
        {"label": "Resumes Ready", "value": str(resume_ready_count)},
        {"label": "Applied", "value": str(applied_count)},
        {"label": "Job Emails", "value": str(email_contact_count)},
        {"label": "Interviews", "value": str(interview_count)},
    ]

    dashboard_sections = [
        {
            "slug": "find_jobs",
            "title": "Find Jobs",
            "description": "Search LinkedIn and save new jobs into the database.",
            "last_run": _format_last_run(latest_intake),
            "card_lines": [
                f"Last Run - {_format_last_run(latest_intake) or 'Never'}",
                f"{_intake_new_jobs_count(latest_intake)} New Jobs Found",
            ],
            "stats": [
                f"{total_jobs} total jobs stored in the local database.",
                f"{best_match_count} jobs currently meet the score threshold.",
            ],
            "cta_label": "Open Find Jobs & Run Agent",
            "cta_href": "/jobs/all/",
            "cta_action": "job_intake",
        },
        {
            "slug": "best_job_matches",
            "title": "Best Job Matches",
            "description": "Review top-scored jobs and generate tailored resumes.",
            "last_run": _format_last_run(latest_scoring),
            "card_lines": [
                f"Last Run - {_format_last_run(latest_scoring) or 'Never'}",
                f"{best_match_count} Jobs Matched",
            ],
            "stats": [
                f"{best_match_count} jobs currently qualify as best matches.",
                f"{resume_ready_count} best matches already have a generated resume PDF.",
            ],
            "cta_label": "Open Best Job Matches & Run Scoring",
            "cta_href": "/jobs/",
            "cta_action": "job_scoring",
        },
        {
            "slug": "applications",
            "title": "Applications",
            "description": "Track submitted applications and resume activity.",
            "last_run": _format_last_run(latest_apply or latest_resume),
            "card_lines": [
                f"Last Run - {_format_last_run(latest_apply or latest_resume) or 'Never'}",
                f"{applied_count} Applications Submitted",
            ],
            "stats": [
                f"{applied_count} jobs are currently marked as applied.",
                f"{resume_ready_count} resumes have been generated.",
            ],
            "cta_label": "Open Applications",
            "cta_href": "/applications/",
            "cta_action": None,
        },
        {
            "slug": "interviews",
            "title": "Emails/Interviews",
            "description": "Track applied jobs, outreach progress, and interview follow-ups.",
            "last_run": _format_datetime_value(latest_email_sent),
            "card_lines": [
                f"Last Run - {_format_datetime_value(latest_email_sent) or 'Never'}",
                f"{emailed_job_count} Jobs Emailed",
            ],
            "stats": [
                f"{applied_count} applied jobs are ready for outreach and interview tracking.",
                f"{emailed_job_count} jobs already have at least one sent email recorded.",
            ],
            "cta_label": "Open Emails/Interviews",
            "cta_href": "/interviews/",
            "cta_action": None,
        },
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_tab": "dashboard",
            "overview_stats": overview_stats,
            "dashboard_sections": dashboard_sections,
        },
    )


def _latest_run(runs: list[dict], agent_name: str) -> dict | None:
    for run in runs:
        if run.get("agent_name") == agent_name:
            return run
    return None


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "unknown time"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%m/%d/%y at %I:%M %p")


def _format_last_run(run: dict | None) -> str | None:
    if not run:
        return None
    if run.get("status") in {"queued", "running"}:
        return "Running now"
    started = run.get("started_at")
    if started:
        return _format_timestamp(started)
    return None


def _format_datetime_value(value) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        return _format_timestamp(value)
    try:
        return value.astimezone().strftime("%m/%d/%y at %I:%M %p")
    except AttributeError:
        return str(value)


def _intake_new_jobs_count(run: dict | None) -> int:
    if not run:
        return 0
    result = run.get("result")
    if not isinstance(result, dict):
        return 0
    raw_value = result.get("accepted_count")
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return 0
