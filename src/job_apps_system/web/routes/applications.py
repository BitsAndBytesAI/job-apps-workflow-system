from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def applications_page(request: Request):
    return templates.TemplateResponse(
        request,
        "workflow_placeholder.html",
        {
            "active_tab": "applications",
            "page_title": "Applications",
            "page_description": "Submitted applications and downstream workflow steps will live here.",
            "placeholder_heading": "Applications screen is next.",
            "placeholder_message": "This tab is reserved for the dedicated submitted-applications workflow.",
        },
    )
