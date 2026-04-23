from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from job_apps_system.config.secrets import get_secret_helper_status
from job_apps_system.schemas.schedule import (
    AgentScheduleConfig,
    AgentScheduleView,
    SCHEDULE_AGENT_LABELS,
    SCHEDULE_AGENT_NAMES,
    SCHEDULE_DAY_OPTIONS,
    SchedulerConfigPayload,
    SchedulerPageState,
    SchedulerTickResult,
)
from job_apps_system.services.launch_agent import scheduler_launch_agent_status
from job_apps_system.services.scheduled_runs import run_scheduled_agent
from job_apps_system.services.setup_config import get_json_setting, set_json_setting


SCHEDULER_CONFIG_KEY = "scheduler_config"
DAY_TO_INDEX = {name: index for index, name in enumerate(SCHEDULE_DAY_OPTIONS)}


def load_scheduler_config(session: Session) -> SchedulerConfigPayload:
    payload = get_json_setting(session, SCHEDULER_CONFIG_KEY, {})
    schedules: list[AgentScheduleConfig] = []
    if isinstance(payload, dict):
        raw_schedules = payload.get("schedules", [])
        if isinstance(raw_schedules, list):
            for item in raw_schedules:
                try:
                    schedules.append(AgentScheduleConfig.model_validate(item))
                except Exception:
                    continue

    by_agent = {item.agent_name: item for item in schedules}
    normalized = [by_agent.get(agent_name) or _default_schedule(agent_name) for agent_name in SCHEDULE_AGENT_NAMES]
    return SchedulerConfigPayload(schedules=normalized)


def save_scheduler_config(session: Session, payload: SchedulerConfigPayload) -> SchedulerConfigPayload:
    normalized = SchedulerConfigPayload.model_validate(payload.model_dump(mode="json"))
    set_json_setting(
        session,
        SCHEDULER_CONFIG_KEY,
        normalized.model_dump(mode="json"),
    )
    return load_scheduler_config(session)


def build_scheduler_page_state(session: Session) -> SchedulerPageState:
    config = load_scheduler_config(session)
    now = datetime.now().astimezone()
    schedules = [
        AgentScheduleView(
            **item.model_dump(mode="json"),
            display_name=SCHEDULE_AGENT_LABELS[item.agent_name],
            next_run_local=_next_run_local(item, now),
        )
        for item in config.schedules
    ]
    return SchedulerPageState(
        schedules=schedules,
        launch_agent=scheduler_launch_agent_status(),
        helper=get_secret_helper_status(session=session),
    )


def run_scheduler_tick(
    session: Session,
    *,
    force_agent_name: str | None = None,
    now: datetime | None = None,
) -> SchedulerTickResult:
    config = load_scheduler_config(session)
    now_local = (now or datetime.now()).astimezone()
    result = SchedulerTickResult()

    for schedule in config.schedules:
        if force_agent_name and schedule.agent_name != force_agent_name:
            result.skipped_agents.append(schedule.agent_name)
            continue

        due = force_agent_name == schedule.agent_name or _schedule_due(schedule, now_local)
        if not due:
            result.skipped_agents.append(schedule.agent_name)
            continue

        run = run_scheduled_agent(schedule.agent_name)
        schedule.last_triggered_slot = _slot_key(now_local)
        schedule.last_run_started_at = run.get("started_at")
        schedule.last_run_finished_at = run.get("finished_at")
        schedule.last_run_status = run.get("status")
        schedule.last_run_message = run.get("message")
        result.triggered_agents.append(schedule.agent_name)
        result.messages.append(run.get("message") or f"Ran {SCHEDULE_AGENT_LABELS[schedule.agent_name]}.")

    save_scheduler_config(session, config)
    return result


def _default_schedule(agent_name: str) -> AgentScheduleConfig:
    return AgentScheduleConfig(
        agent_name=agent_name,
        enabled=False,
        days_of_week=["mon", "tue", "wed", "thu", "fri"],
        run_at_local_time="09:00",
    )


def _schedule_due(schedule: AgentScheduleConfig, now_local: datetime) -> bool:
    if not schedule.enabled or not schedule.days_of_week:
        return False
    current_day = SCHEDULE_DAY_OPTIONS[now_local.weekday()]
    if current_day not in schedule.days_of_week:
        return False
    if now_local.strftime("%H:%M") != schedule.run_at_local_time:
        return False
    return schedule.last_triggered_slot != _slot_key(now_local)


def _slot_key(now_local: datetime) -> str:
    return now_local.strftime("%Y-%m-%dT%H:%M")


def _next_run_local(schedule: AgentScheduleConfig, now_local: datetime) -> str | None:
    if not schedule.enabled or not schedule.days_of_week:
        return None
    hour, minute = [int(part) for part in schedule.run_at_local_time.split(":", 1)]
    allowed_days = {DAY_TO_INDEX[day] for day in schedule.days_of_week if day in DAY_TO_INDEX}
    for offset in range(0, 8):
        candidate_date = (now_local + timedelta(days=offset)).date()
        candidate = datetime.combine(candidate_date, datetime.min.time(), tzinfo=now_local.tzinfo).replace(hour=hour, minute=minute)
        if candidate.weekday() not in allowed_days:
            continue
        if candidate < now_local:
            continue
        return candidate.strftime("%a %b %-d, %H:%M")
    return None
