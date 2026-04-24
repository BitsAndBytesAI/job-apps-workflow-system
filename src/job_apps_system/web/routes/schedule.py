from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from job_apps_system.db.session import get_db_session
from job_apps_system.schemas.schedule import SchedulerConfigPayload, SchedulerPageState
from job_apps_system.services.launch_agent import (
    install_scheduler_launch_agent,
    uninstall_scheduler_launch_agent,
)
from job_apps_system.services.scheduler import (
    build_scheduler_page_state,
    load_scheduler_config,
    run_scheduler_tick,
    save_scheduler_config,
)


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def schedule_page(request: Request):
    return templates.TemplateResponse(request, "schedule.html", {"active_tab": "schedule"})


@router.get("/api/config")
def get_schedule_config() -> SchedulerPageState:
    with get_db_session() as session:
        return build_scheduler_page_state(session)


@router.put("/api/config")
def put_schedule_config(payload: SchedulerConfigPayload) -> SchedulerPageState:
    with get_db_session() as session:
        save_scheduler_config(session, payload)
        return build_scheduler_page_state(session)


@router.post("/api/install")
def install_schedule_background_item() -> SchedulerPageState:
    try:
        install_scheduler_launch_agent()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with get_db_session() as session:
        return build_scheduler_page_state(session)


@router.post("/api/uninstall")
def uninstall_schedule_background_item() -> SchedulerPageState:
    uninstall_scheduler_launch_agent()
    with get_db_session() as session:
        return build_scheduler_page_state(session)


@router.post("/api/tick")
def tick_schedule() -> dict:
    with get_db_session() as session:
        result = run_scheduler_tick(session)
        state = build_scheduler_page_state(session)
        return {"result": result.model_dump(mode="json"), "state": state.model_dump(mode="json")}


@router.post("/api/run/{agent_name}")
def run_schedule_agent_now(agent_name: str) -> dict:
    with get_db_session() as session:
        config = load_scheduler_config(session)
        if agent_name not in {item.agent_name for item in config.schedules}:
            raise HTTPException(status_code=404, detail="Scheduled agent not found.")
        result = run_scheduler_tick(session, force_agent_name=agent_name)
        state = build_scheduler_page_state(session)
        return {"result": result.model_dump(mode="json"), "state": state.model_dump(mode="json")}
