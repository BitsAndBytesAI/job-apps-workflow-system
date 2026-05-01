from __future__ import annotations

from typing import Any

from job_apps_system.agents.job_intake import JobIntakeAgent
from job_apps_system.agents.job_scoring import JobScoringAgent
from job_apps_system.agents.resume_generation import ResumeGenerationAgent
from job_apps_system.db.session import SessionLocal
from job_apps_system.schemas.schedule import SCHEDULE_AGENT_LABELS, SCHEDULE_AGENT_NAMES
from job_apps_system.services.manual_runs import (
    create_manual_run,
    finalize_run_with_retry,
    update_active_run,
)
from job_apps_system.services.setup_config import load_setup_config


def run_scheduled_agent(agent_name: str) -> dict[str, Any]:
    if agent_name not in SCHEDULE_AGENT_NAMES:
        raise ValueError(f"Unsupported scheduled agent: {agent_name}")

    with SessionLocal() as session:
        project_id = load_setup_config(session).app.project_id
        run = create_manual_run(
            session,
            agent_name=agent_name,
            project_id=project_id,
            trigger_type="scheduled",
            trigger_source="scheduler_tick",
        )
        session.commit()

    run_id = run["id"]
    update_active_run(run_id, status="running", message=f"Starting {SCHEDULE_AGENT_LABELS[agent_name]}.")

    def step_reporter(*args, **kwargs) -> None:
        name = kwargs.get("name")
        status = kwargs.get("status")
        message = kwargs.get("message")
        if len(args) == 3 and not kwargs:
            name, status, message = args
        if not name:
            name = "run"
        if not status:
            status = "running"
        update_active_run(
            run_id,
            status="failed" if status == "failed" else "running",
            message=message or f"{SCHEDULE_AGENT_LABELS[agent_name]} is running.",
            step_name=name,
            step_status=status,
        )

    try:
        with SessionLocal() as session:
            try:
                config = load_setup_config(session)
                summary = _run_agent(
                    session=session,
                    config=config,
                    agent_name=agent_name,
                    step_reporter=step_reporter,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
        final_status = "cancelled" if getattr(summary, "cancelled", False) else ("succeeded" if summary.ok else "failed")
        return finalize_run_with_retry(
            run_id,
            status=final_status,
            message=summary.message,
            result=summary.model_dump(mode="json"),
            error=None if summary.ok or getattr(summary, "cancelled", False) else summary.message,
        )
    except Exception as exc:
        return finalize_run_with_retry(
            run_id,
            status="failed",
            message=str(exc),
            error=str(exc),
        )


def _run_agent(*, session, config, agent_name: str, step_reporter):
    if agent_name == "job_intake":
        return JobIntakeAgent(session).run(
            search_urls=config.linkedin.search_urls or None,
            max_jobs_per_search=config.app.max_jobs_per_run,
            step_reporter=step_reporter,
            cancel_checker=lambda: False,
        )
    if agent_name == "job_scoring":
        return JobScoringAgent(session).run(
            limit=None,
            job_ids=None,
            step_reporter=step_reporter,
            cancel_checker=lambda: False,
        )
    if agent_name == "resume_generation":
        return ResumeGenerationAgent(session).run(
            limit=None,
            job_ids=None,
            step_reporter=step_reporter,
            cancel_checker=lambda: False,
        )
    raise ValueError(f"Unsupported scheduled agent: {agent_name}")
