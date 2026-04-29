from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from job_apps_system.config.models import (
    ANTHROPIC_MODEL_OPTIONS,
    ApplicantProfileConfig,
    JOB_SITE_OPTIONS,
    OPENAI_MODEL_OPTIONS,
    SetupConfig,
)
from job_apps_system.config.secrets import delete_secret
from job_apps_system.db.session import get_db_session
from job_apps_system.integrations.google.oauth import get_google_auth_status
from job_apps_system.integrations.linkedin.auth import get_linkedin_auth_status
from job_apps_system.services.project_resume import (
    project_resume_config_from_file,
    resolve_resume_from_url,
    store_uploaded_docx,
)
from job_apps_system.services.setup_config import build_setup_update, load_setup_config, save_setup_config
from job_apps_system.services.setup_config import with_live_connection_status
from job_apps_system.agents.resume_generation import ResumeGenerationAgent


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

WIZARD_STEPS = [
    ("project", "Job Title"),
    ("resume", "Base Resume"),
    ("job-sites", "Job Sites"),
    ("models", "AI Models"),
    ("anymailfinder", "Anymailfinder"),
    ("score-threshold", "Scoring Threshold"),
    ("applicant", "Applicant Profile"),
    ("google", "Google Connect"),
]


class ProjectStepPayload(BaseModel):
    job_role: str


class ResumeLinkPayload(BaseModel):
    source_url: str


class JobSitesPayload(BaseModel):
    selected_job_sites: list[str]
    search_url: str | None = None


class ModelsStepPayload(BaseModel):
    openai_model: str
    anthropic_model: str
    openai_api_key: str
    anthropic_api_key: str


class OptionalApiKeyPayload(BaseModel):
    api_key: str | None = None


class ScoreThresholdPayload(BaseModel):
    score_threshold: int


class ApplicantProfilePayload(ApplicantProfileConfig):
    pass


@router.get("/", response_class=HTMLResponse)
def onboarding_page(request: Request):
    with get_db_session() as session:
        config = load_setup_config(session)
        config = with_live_connection_status(config, session)
        if config.onboarding.wizard_completed:
            return RedirectResponse(url="/", status_code=303)

    current_step = config.onboarding.wizard_current_step
    available_step_ids = {step_id for step_id, _ in WIZARD_STEPS}
    if current_step not in available_step_ids:
        current_step = WIZARD_STEPS[0][0]

    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "wizard_steps": WIZARD_STEPS,
            "current_step": current_step,
            "current_step_index": _step_index(current_step),
            "config": config,
            "openai_model_options": OPENAI_MODEL_OPTIONS,
            "anthropic_model_options": ANTHROPIC_MODEL_OPTIONS,
            "job_site_options": JOB_SITE_OPTIONS,
        },
    )


@router.get("/api/state")
def onboarding_state() -> SetupConfig:
    with get_db_session() as session:
        config = load_setup_config(session)
        return with_live_connection_status(config, session)


@router.post("/api/back")
def onboarding_back() -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        current_step = config.onboarding.wizard_current_step
        current_index = _step_index(current_step)
        previous_step = WIZARD_STEPS[max(current_index - 1, 0)][0]
        update = build_setup_update(config)
        update.onboarding.wizard_current_step = previous_step
        saved = save_setup_config(session, update)
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/project")
def save_project_step(payload: ProjectStepPayload) -> dict:
    if not payload.job_role.strip():
        raise HTTPException(status_code=400, detail="Job title is required.")

    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        generated_project_name = _generate_project_name(payload.job_role)
        update.app.project_name = generated_project_name
        update.app.job_role = payload.job_role.strip()
        update.app.project_id = generated_project_name
        update.onboarding.wizard_current_step = _next_step_id("project")
        saved = save_setup_config(session, update)
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/resume/link")
def save_resume_link(payload: ResumeLinkPayload) -> dict:
    source_url = payload.source_url.strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="Resume link is required.")

    with get_db_session() as session:
        config = load_setup_config(session)
        if (
            source_url == (config.project_resume.source_url or "").strip()
            and (config.project_resume.extracted_text or "").strip()
        ):
            return _advance_resume_step(session, config)

        update = build_setup_update(config)
        update.project_resume.source_url = source_url
        update.project_resume.original_file_name = None
        update.project_resume.original_file_path = None
        update.project_resume.extracted_text = None

        try:
            resolved = resolve_resume_from_url(
                session=session,
                project_id=config.app.project_id,
                source_url=source_url,
            )
        except Exception as exc:
            detail = str(exc)
            if _looks_like_google_doc_url(source_url):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "google_doc_access_required",
                        "message": "Please make your resume doc publicly accessible or connect your Google Account.",
                    },
                ) from exc
            raise HTTPException(status_code=400, detail=detail) from exc

        update.project_resume = project_resume_config_from_file(resolved)

        update.onboarding.wizard_current_step = _next_step_id("resume")
        saved = save_setup_config(session, update)
        return {
            "ok": True,
            "current_step": saved.onboarding.wizard_current_step,
            "resume_extracted": bool(saved.project_resume.extracted_text),
        }


@router.post("/api/resume/continue")
def continue_resume_step() -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        if not (config.project_resume.extracted_text or "").strip():
            raise HTTPException(status_code=400, detail="Upload a .docx file or provide a readable resume link.")
        return _advance_resume_step(session, config)


@router.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Upload a Word .docx resume file.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded resume file is empty.")

    with get_db_session() as session:
        config = load_setup_config(session)
        stored = store_uploaded_docx(
            project_id=config.app.project_id,
            filename=file.filename,
            content=content,
        )
        update = build_setup_update(config)
        update.project_resume = project_resume_config_from_file(stored)
        update.onboarding.wizard_current_step = _next_step_id("resume")
        saved = save_setup_config(session, update)
        return {
            "ok": True,
            "current_step": saved.onboarding.wizard_current_step,
            "file_name": saved.project_resume.original_file_name,
        }


@router.post("/api/job-sites")
def save_job_sites_step(payload: JobSitesPayload) -> dict:
    normalized = [site.strip().lower() for site in payload.selected_job_sites if site.strip()]
    if "linkedin" not in normalized:
        raise HTTPException(status_code=400, detail="Select LinkedIn to continue.")

    search_url = (payload.search_url or "").strip()
    if not search_url:
        raise HTTPException(status_code=400, detail="LinkedIn search URL is required.")
    if "linkedin.com/jobs" not in search_url.lower():
        raise HTTPException(status_code=400, detail="Enter a valid LinkedIn jobs search URL.")

    with get_db_session() as session:
        config = load_setup_config(session)
        linkedin_status = get_linkedin_auth_status(config.linkedin.browser_profile_path)
        if not linkedin_status.get("authenticated"):
            raise HTTPException(status_code=400, detail="Connect LinkedIn before continuing.")

        update = build_setup_update(config)
        update.app.selected_job_sites = normalized
        update.linkedin.search_urls = [search_url]
        update.onboarding.wizard_current_step = _next_step_id("job-sites")
        saved = save_setup_config(session, update)
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/models")
def save_models_step(payload: ModelsStepPayload) -> dict:
    if payload.openai_model not in OPENAI_MODEL_OPTIONS:
        raise HTTPException(status_code=400, detail="Select a valid OpenAI model.")
    if payload.anthropic_model not in ANTHROPIC_MODEL_OPTIONS:
        raise HTTPException(status_code=400, detail="Select a valid Anthropic model.")

    with get_db_session() as session:
        config = load_setup_config(session)
        _raise_for_helper_health(config)
        openai_api_key = payload.openai_api_key.strip()
        anthropic_api_key = payload.anthropic_api_key.strip()
        if not openai_api_key and not config.secrets.openai_api_key_configured:
            _raise_for_secret(config.secrets.openai_api_key, fallback_message="OpenAI API key is required.")
        if not anthropic_api_key and not config.secrets.anthropic_api_key_configured:
            _raise_for_secret(config.secrets.anthropic_api_key, fallback_message="Anthropic API key is required.")
        update = build_setup_update(config)
        update.models.openai_model = payload.openai_model
        update.models.anthropic_model = payload.anthropic_model
        update.secrets.openai_api_key = openai_api_key or None
        update.secrets.anthropic_api_key = anthropic_api_key or None
        update.onboarding.wizard_current_step = _next_step_id("models")
        try:
            saved = save_setup_config(session, update)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": config.secrets.helper.last_error_code or "helper_runtime_failure",
                    "message": str(exc),
                },
            ) from exc
        _raise_for_helper_health(saved)
        _raise_for_secret(saved.secrets.openai_api_key, fallback_message="OpenAI API key is required.")
        _raise_for_secret(saved.secrets.anthropic_api_key, fallback_message="Anthropic API key is required.")
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/anymailfinder")
def save_anymailfinder_step(payload: OptionalApiKeyPayload) -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        _raise_for_helper_health(config)
        update = build_setup_update(config)
        api_key = (payload.api_key or "").strip()
        update.secrets.anymailfinder_api_key = api_key or None
        if api_key:
            update.app.send_enabled = True
        elif config.secrets.anymailfinder_api_key_configured:
            if not delete_secret("anymailfinder_api_key", session=session):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": config.secrets.helper.last_error_code or "helper_runtime_failure",
                        "message": "Unable to clear the Anymailfinder API key.",
                    },
                )
            update.app.send_enabled = False
        elif not config.secrets.anymailfinder_api_key_configured:
            update.app.send_enabled = False
        update.onboarding.wizard_current_step = _next_step_id("anymailfinder")
        try:
            saved = save_setup_config(session, update)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": config.secrets.helper.last_error_code or "helper_runtime_failure",
                    "message": str(exc),
                },
            ) from exc
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/score-threshold")
def save_score_threshold_step(payload: ScoreThresholdPayload) -> dict:
    if payload.score_threshold < 0 or payload.score_threshold > 1000:
        raise HTTPException(status_code=400, detail="Score threshold must be between 0 and 1000.")

    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        update.app.score_threshold = payload.score_threshold
        update.onboarding.wizard_current_step = _next_step_id("score-threshold")
        saved = save_setup_config(session, update)
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/applicant")
def save_applicant_step(payload: ApplicantProfilePayload) -> dict:
    missing = _missing_applicant_fields(payload)
    if missing:
        raise HTTPException(status_code=400, detail=f"Applicant profile is missing: {', '.join(missing)}.")

    with get_db_session() as session:
        config = load_setup_config(session)
        update = build_setup_update(config)
        update.applicant = ApplicantProfileConfig.model_validate(payload.model_dump())
        update.onboarding.wizard_current_step = _next_step_id("applicant")
        saved = save_setup_config(session, update)
        return {"ok": True, "current_step": saved.onboarding.wizard_current_step}


@router.post("/api/google/complete")
def complete_google_step() -> dict:
    with get_db_session() as session:
        config = load_setup_config(session)
        _raise_for_helper_health(config)
        google_status = get_google_auth_status(session)
        if not google_status.connected:
            raise HTTPException(status_code=400, detail="Connect Google before finishing onboarding.")
        if "linkedin" not in config.app.selected_job_sites:
            raise HTTPException(status_code=400, detail="Select and connect at least one job site before finishing onboarding.")
        if not config.linkedin.search_urls:
            raise HTTPException(status_code=400, detail="LinkedIn search URL is required.")
        _raise_for_secret(config.secrets.openai_api_key, fallback_message="OpenAI API key is required.")
        _raise_for_secret(config.secrets.anthropic_api_key, fallback_message="Anthropic API key is required.")
        missing_applicant_fields = _missing_applicant_fields(config.applicant)
        if missing_applicant_fields:
            raise HTTPException(status_code=400, detail=f"Applicant profile is missing: {', '.join(missing_applicant_fields)}.")
        if not (config.project_resume.extracted_text or "").strip():
            source_url = (config.project_resume.source_url or "").strip()
            if source_url:
                resolved = resolve_resume_from_url(
                    session=session,
                    project_id=config.app.project_id,
                    source_url=source_url,
                )
                update = build_setup_update(config)
                update.project_resume = project_resume_config_from_file(resolved)
                config = save_setup_config(session, update)
            if not (config.project_resume.extracted_text or "").strip():
                raise HTTPException(status_code=400, detail="Base resume content could not be extracted. Upload a .docx file or use a readable Google Docs link.")

        ResumeGenerationAgent(session).ensure_managed_folders()
        update = build_setup_update(config)
        update.onboarding.wizard_completed = True
        update.onboarding.wizard_current_step = "complete"
        saved = save_setup_config(session, update)
        return {"ok": True, "wizard_completed": saved.onboarding.wizard_completed, "redirect_to": "/"}


def _step_index(step_id: str) -> int:
    for index, (candidate, _) in enumerate(WIZARD_STEPS):
        if candidate == step_id:
            return index
    return 0


def _next_step_id(step_id: str) -> str:
    index = _step_index(step_id)
    if index >= len(WIZARD_STEPS) - 1:
        return WIZARD_STEPS[-1][0]
    return WIZARD_STEPS[index + 1][0]


def _generate_project_name(job_role: str) -> str:
    return job_role.strip()


def _looks_like_google_doc_url(source_url: str) -> bool:
    return "docs.google.com/document" in source_url or "/document/d/" in source_url


def _advance_resume_step(session, config: SetupConfig) -> dict:
    update = build_setup_update(config)
    update.onboarding.wizard_current_step = _next_step_id("resume")
    saved = save_setup_config(session, update)
    return {
        "ok": True,
        "current_step": saved.onboarding.wizard_current_step,
        "resume_extracted": bool(saved.project_resume.extracted_text),
    }


def _missing_applicant_fields(applicant: ApplicantProfileConfig) -> list[str]:
    required = {
        "Legal Name": applicant.legal_name,
        "Email": applicant.email,
        "Phone": applicant.phone,
        "LinkedIn URL": applicant.linkedin_url,
        "City": applicant.city,
        "State": applicant.state,
        "Country": applicant.country,
        "Compensation Expectation": applicant.compensation_expectation,
    }
    return [label for label, value in required.items() if not str(value or "").strip()]
def _raise_for_helper_health(config: SetupConfig) -> None:
    helper = config.secrets.helper
    if helper.backend != "native_helper" or helper.healthy:
        return
    raise HTTPException(
        status_code=400,
        detail={
            "code": helper.last_error_code or "helper_runtime_failure",
            "message": helper.status_message,
        },
    )


def _raise_for_secret(secret_status, *, fallback_message: str) -> None:
    if getattr(secret_status, "configured", False):
        return
    raise HTTPException(
        status_code=400,
        detail={
            "code": getattr(secret_status, "status_code", None) or "missing_secret",
            "message": getattr(secret_status, "status_message", None) or fallback_message,
        },
    )
