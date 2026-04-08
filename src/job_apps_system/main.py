from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from job_apps_system.db.session import init_db
from job_apps_system.web.routes.dashboard import router as dashboard_router
from job_apps_system.web.routes.jobs import router as jobs_router
from job_apps_system.web.routes.resumes import router as resumes_router
from job_apps_system.web.routes.runs import router as runs_router
from job_apps_system.web.routes.scoring import router as scoring_router
from job_apps_system.web.routes.setup import router as setup_router
from job_apps_system.web.routes.interviews import router as interviews_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Job Apps Workflow System", lifespan=lifespan)
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthcheck() -> dict[str, str | bool]:
        return {"ok": True, "status": "ready"}

    @app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
    async def chrome_devtools_probe() -> Response:
        return Response(status_code=204)

    app.include_router(dashboard_router)
    app.include_router(setup_router, prefix="/setup", tags=["setup"])
    app.include_router(runs_router, prefix="/runs", tags=["runs"])
    app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
    app.include_router(scoring_router, prefix="/scoring", tags=["scoring"])
    app.include_router(resumes_router, prefix="/resumes", tags=["resumes"])
    app.include_router(interviews_router, prefix="/interviews", tags=["interviews"])
    return app


app = create_app()
