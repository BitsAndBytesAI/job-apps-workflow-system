from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from job_apps_system.config.models import SecretHelperStatus


SCHEDULE_DAY_OPTIONS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SCHEDULE_AGENT_NAMES = ["job_intake", "job_scoring", "resume_generation"]
SCHEDULE_AGENT_LABELS = {
    "job_intake": "Jobs Agent",
    "job_scoring": "Scoring Agent",
    "resume_generation": "Resume Agent",
}


class AgentScheduleConfig(BaseModel):
    agent_name: str
    enabled: bool = False
    days_of_week: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    run_at_local_time: str = "09:00"
    last_triggered_slot: str | None = None
    last_run_started_at: str | None = None
    last_run_finished_at: str | None = None
    last_run_status: str | None = None
    last_run_message: str | None = None

    @field_validator("agent_name")
    @classmethod
    def validate_agent_name(cls, value: str) -> str:
        candidate = (value or "").strip()
        if candidate not in SCHEDULE_AGENT_NAMES:
            raise ValueError("Unsupported scheduled agent.")
        return candidate

    @field_validator("days_of_week")
    @classmethod
    def validate_days(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            candidate = (item or "").strip().lower()
            if candidate not in SCHEDULE_DAY_OPTIONS or candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)
        return normalized

    @field_validator("run_at_local_time")
    @classmethod
    def validate_time(cls, value: str) -> str:
        candidate = (value or "").strip()
        parts = candidate.split(":")
        if len(parts) != 2:
            raise ValueError("Time must use HH:MM.")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("Time must use HH:MM.")
        return f"{hour:02d}:{minute:02d}"


class SchedulerConfigPayload(BaseModel):
    schedules: list[AgentScheduleConfig] = Field(default_factory=list)


class LaunchAgentStatus(BaseModel):
    label: str
    installed: bool
    loaded: bool
    plist_path: str | None = None
    status_message: str = ""


class AgentScheduleView(AgentScheduleConfig):
    display_name: str
    next_run_local: str | None = None


class SchedulerPageState(BaseModel):
    schedules: list[AgentScheduleView] = Field(default_factory=list)
    launch_agent: LaunchAgentStatus
    helper: SecretHelperStatus


class SchedulerTickResult(BaseModel):
    triggered_agents: list[str] = Field(default_factory=list)
    skipped_agents: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
