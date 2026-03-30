from fastapi import FastAPI

from job_apps_system.web.routes.dashboard import router as dashboard_router
from job_apps_system.web.routes.jobs import router as jobs_router
from job_apps_system.web.routes.runs import router as runs_router
from job_apps_system.web.routes.setup import router as setup_router
from job_apps_system.web.routes.interviews import router as interviews_router


def create_app() -> FastAPI:
    app = FastAPI(title="Job Apps Workflow System")
    app.include_router(dashboard_router)
    app.include_router(setup_router, prefix="/setup", tags=["setup"])
    app.include_router(runs_router, prefix="/runs", tags=["runs"])
    app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
    app.include_router(interviews_router, prefix="/interviews", tags=["interviews"])
    return app


app = create_app()
