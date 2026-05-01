from fastapi import APIRouter, HTTPException

from job_apps_system.db.session import get_db_session
from job_apps_system.schemas.schedule import (
    AgentScheduleConfig,
    SchedulerConfigPayload,
    SchedulerPageState,
)
from job_apps_system.services.launch_agent import install_scheduler_launch_agent, uninstall_scheduler_launch_agent
from job_apps_system.services.scheduler import (
    build_scheduler_page_state,
    load_scheduler_config,
    save_scheduler_config,
)


router = APIRouter()


@router.get("/agent/{agent_name}")
def get_agent_schedule(agent_name: str) -> AgentScheduleConfig:
    with get_db_session() as session:
        config = load_scheduler_config(session)
        for entry in config.schedules:
            if entry.agent_name == agent_name:
                return entry
    raise HTTPException(status_code=404, detail="Scheduled agent not found.")


@router.put("/agent/{agent_name}")
def put_agent_schedule(agent_name: str, payload: AgentScheduleConfig) -> SchedulerPageState:
    if payload.agent_name != agent_name:
        raise HTTPException(status_code=400, detail="Agent name mismatch.")
    with get_db_session() as session:
        config = load_scheduler_config(session)
        updated: list[AgentScheduleConfig] = []
        replaced = False
        for entry in config.schedules:
            if entry.agent_name == agent_name:
                merged = payload.model_copy(update={
                    "last_triggered_slot": entry.last_triggered_slot,
                    "last_run_started_at": entry.last_run_started_at,
                    "last_run_finished_at": entry.last_run_finished_at,
                    "last_run_status": entry.last_run_status,
                    "last_run_message": entry.last_run_message,
                })
                updated.append(merged)
                replaced = True
            else:
                updated.append(entry)
        if not replaced:
            updated.append(payload)
        save_scheduler_config(session, SchedulerConfigPayload(schedules=updated))
        try:
            if any(entry.enabled for entry in updated):
                install_scheduler_launch_agent()
            else:
                uninstall_scheduler_launch_agent()
        except RuntimeError:
            pass
        return build_scheduler_page_state(session)
