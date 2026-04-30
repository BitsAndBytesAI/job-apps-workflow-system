from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from job_apps_system.web.templating import templates


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def communications_page(request: Request):
    return templates.TemplateResponse(request, "communications.html", {"active_tab": "communications"})
