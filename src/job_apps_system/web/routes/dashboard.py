from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from job_apps_system.db.models.jobs import Job
from job_apps_system.db.session import get_db_session
from job_apps_system.services.manual_runs import list_runs as list_run_records
from job_apps_system.services.setup_config import load_setup_config


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


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
    ]

    dashboard_sections = [
        {
            "slug": "find_jobs",
            "title": "Find Jobs",
            "description": "Search LinkedIn and save new jobs into the database.",
            "last_run": _format_last_run(latest_intake),
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
            "title": "Interviews",
            "description": "Schedule interviews and automate follow-ups.",
            "last_run": None,
            "stats": [
                "No interviews scheduled yet.",
                "Interview automation is not wired yet.",
            ],
            "cta_label": "Open Interviews",
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


def _run_result(run: dict | None) -> dict:
    if not run:
        return {}
    result = run.get("result")
    return result if isinstance(result, dict) else {}


def _format_last_run(run: dict | None) -> str | None:
    if not run:
        return None
    if run.get("status") in {"queued", "running"}:
        return "Running now"
    started = run.get("started_at")
    if started:
        return _format_timestamp(started)
    return None


def _format_intake_stat(run: dict | None) -> str:
    if not run:
        return "Find Jobs has not been run yet."
    if run.get("status") in {"queued", "running"}:
        return f"Find Jobs is running now. Started {_format_timestamp(run.get('started_at'))}."
    result = _run_result(run)
    processed = result.get("processed_count")
    if processed is not None:
        return f"Last Find Jobs run on {_format_timestamp(run.get('started_at'))} found {processed} new job(s)."
    return f"Last Find Jobs run on {_format_timestamp(run.get('started_at'))}."


def _format_scoring_stat(run: dict | None) -> str:
    if not run:
        return "Scoring has not been run yet."
    if run.get("status") in {"queued", "running"}:
        return f"Scoring is running now. Started {_format_timestamp(run.get('started_at'))}."
    result = _run_result(run)
    scored = result.get("scored_count")
    if scored is not None:
        return f"Last scoring run on {_format_timestamp(run.get('started_at'))} scored {scored} job(s)."
    return f"Last scoring run on {_format_timestamp(run.get('started_at'))}."


def _format_resume_stat(run: dict | None) -> str:
    if not run:
        return "Resume generation has not been run yet."
    if run.get("status") in {"queued", "running"}:
        return f"Resume generation is running now. Started {_format_timestamp(run.get('started_at'))}."
    result = _run_result(run)
    generated = result.get("generated_count")
    if generated is not None:
        return f"Last resume run on {_format_timestamp(run.get('started_at'))} generated {generated} resume(s)."
    return f"Last resume run on {_format_timestamp(run.get('started_at'))}."


def _format_apply_stat(run: dict | None) -> str:
    if not run:
        return "Apply automation has not been run yet."
    if run.get("status") in {"queued", "running"}:
        return f"Apply automation is running now. Started {_format_timestamp(run.get('started_at'))}."
    result = _run_result(run)
    applied = result.get("applied_count")
    if applied is not None:
        return f"Last apply run on {_format_timestamp(run.get('started_at'))} submitted {applied} application(s)."
    return f"Last apply run on {_format_timestamp(run.get('started_at'))}."
