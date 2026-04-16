import logging
from email.utils import parseaddr
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from job_apps_system.config.models import (
    ANTHROPIC_MODEL_OPTIONS,
    FieldValidationRequest,
    FieldValidationResponse,
    GoogleResourceValidationItem,
    GoogleResourceValidationResponse,
    LinkedInAuthStatus,
    LinkedInBrowserLaunchResponse,
    LinkedInBrowserTerminateRequest,
    LinkedInBrowserTerminateResponse,
    OPENAI_MODEL_OPTIONS,
    SetupConfigUpdate,
    SetupValidationResponse,
)
from job_apps_system.db.session import get_db_session
from job_apps_system.integrations.google.drive import validate_drive_resource
from job_apps_system.integrations.google.oauth import (
    complete_google_oauth,
    get_google_auth_status,
    start_google_oauth,
)
from job_apps_system.integrations.linkedin.auth import get_linkedin_auth_status
from job_apps_system.integrations.linkedin.browser import (
    resolve_browser_profile_path,
    spawn_linkedin_browser,
    terminate_linkedin_browser,
)
from job_apps_system.services.setup_config import (
    load_setup_config,
    save_field_validation,
    save_setup_config,
    validate_setup_config,
)


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
logger = logging.getLogger(__name__)


@router.get("/", response_class=HTMLResponse)
def setup_page(request: Request):
    return templates.TemplateResponse(request, "setup.html", {})


@router.get("/api/config")
def get_setup_config():
    with get_db_session() as session:
        return load_setup_config(session)


@router.post("/api/validate")
def validate_setup(payload: SetupConfigUpdate) -> SetupValidationResponse:
    return validate_setup_config(payload)


@router.post("/api/field-validate")
def validate_setup_field(payload: FieldValidationRequest) -> FieldValidationResponse:
    validation = validate_setup_config(payload.payload)
    normalized = validation.normalized
    field_name = payload.field_name

    with get_db_session() as session:
        if field_name.startswith("google.resources."):
            resource_name = field_name.split(".", 2)[2]
            resource_id = getattr(normalized.google.resources, resource_name, None)
            if not resource_id:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Enter a Google URL or ID first.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            if not get_google_auth_status(session).connected:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Connect Google first.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            result = validate_drive_resource(resource_id)
            if result["ok"]:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=True,
                    level="success",
                    message=f'Validated: {result["name"]} ({result["mime_type"]}).',
                    normalized_value=result["resource_id"],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            response = FieldValidationResponse(
                field_name=field_name,
                ok=False,
                level="error",
                message=result["error"] or "Unable to access that Google resource.",
                normalized_value=resource_id,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "linkedin.browser_profile_path":
            path_value = normalized.linkedin.browser_profile_path.strip()
            if not path_value:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Enter a browser profile path first.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            resolved = resolve_browser_profile_path(path_value)
            exists = resolved.exists()
            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success" if exists else "info",
                message=f'Path {"exists" if exists else "will be created"}: {resolved}',
                normalized_value=str(resolved),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "linkedin.search_urls":
            urls = normalized.linkedin.search_urls
            if not urls:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Enter at least one LinkedIn jobs search URL.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            invalid = [url for url in urls if "linkedin.com/jobs" not in url]
            if invalid:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message=f"Invalid jobs URL: {invalid[0]}",
                    normalized_value=len(urls),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=f"Validated {len(urls)} LinkedIn jobs URL(s).",
                normalized_value=len(urls),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name.startswith("models."):
            model_value = _resolve_field_value(normalized, field_name)
            if not model_value:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Enter a model ID first.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            allowed_models = {
                "models.openai_model": OPENAI_MODEL_OPTIONS,
                "models.anthropic_model": ANTHROPIC_MODEL_OPTIONS,
            }.get(field_name, [])
            if allowed_models and model_value not in allowed_models:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Select a model from the dropdown list.",
                    normalized_value=model_value,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=f"Model configured: {model_value}",
                normalized_value=model_value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "app.schedule_minutes":
            value = normalized.app.schedule_minutes
            if value < 1:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Schedule minutes must be at least 1.",
                    normalized_value=value,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response
            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=f"Runs every {value} minute(s).",
                normalized_value=value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "app.job_role":
            value = (normalized.app.job_role or "").strip()
            if not value:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Enter the target job role first.",
                    normalized_value=value,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response
            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=f"Target job role: {value}",
                normalized_value=value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "app.project_id":
            value = (normalized.app.project_id or "").strip()
            if not value:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Enter the project ID first.",
                    normalized_value=value,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response
            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=f"Project ID: {value}",
                normalized_value=value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "app.score_threshold":
            value = normalized.app.score_threshold
            if value < 0 or value > 100:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=False,
                    level="error",
                    message="Score threshold must be between 0 and 100.",
                    normalized_value=value,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response
            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=f"Score threshold set to {value}.",
                normalized_value=value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name in {"app.hide_jobs_below_score_threshold", "app.dry_run", "app.send_enabled"}:
            value = _resolve_field_value(normalized, field_name)
            message = "Enabled" if value else "Disabled"
            response = FieldValidationResponse(
                field_name=field_name,
                ok=True,
                level="success",
                message=message,
                normalized_value=value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name == "app.send_bcc":
            value = normalized.app.send_bcc or ""
            if not value:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=True,
                    level="info",
                    message="No BCC configured.",
                    normalized_value="",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response

            _, parsed_email = parseaddr(value)
            is_valid = "@" in parsed_email
            response = FieldValidationResponse(
                field_name=field_name,
                ok=is_valid,
                level="success" if is_valid else "error",
                message="Valid email address." if is_valid else "Enter a valid email address.",
                normalized_value=value,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

        if field_name.startswith("secrets."):
            secret_name = field_name.split(".", 1)[1]
            secret_value = getattr(payload.payload.secrets, secret_name, None)
            configured = {
                "openai_api_key": normalized.secrets.openai_api_key_configured,
                "anthropic_api_key": normalized.secrets.anthropic_api_key_configured,
                "anymailfinder_api_key": normalized.secrets.anymailfinder_api_key_configured,
            }.get(secret_name, False)
            if secret_value:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=True,
                    level="success",
                    message="Key entered. Save to persist it.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response
            if configured:
                response = FieldValidationResponse(
                    field_name=field_name,
                    ok=True,
                    level="info",
                    message="Key already configured.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                save_field_validation(session, response)
                return response
            response = FieldValidationResponse(
                field_name=field_name,
                ok=False,
                level="error",
                message="No key entered.",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            save_field_validation(session, response)
            return response

    value = _resolve_field_value(normalized, field_name)
    if isinstance(value, str) and not value.strip():
        response = FieldValidationResponse(
            field_name=field_name,
            ok=False,
            level="error",
            message="This field is empty.",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        with get_db_session() as session:
            save_field_validation(session, response)
        return response

    response = FieldValidationResponse(
        field_name=field_name,
        ok=True,
        level="success",
        message="Value looks valid.",
        normalized_value=value,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    with get_db_session() as session:
        save_field_validation(session, response)
    return response


@router.put("/api/config")
def put_setup_config(payload: SetupConfigUpdate):
    with get_db_session() as session:
        return save_setup_config(session, payload)


@router.post("/api/linkedin/browser/launch")
def linkedin_browser_launch(payload: SetupConfigUpdate) -> LinkedInBrowserLaunchResponse:
    result = spawn_linkedin_browser(payload.linkedin.browser_profile_path)
    return LinkedInBrowserLaunchResponse(**result)


@router.post("/api/linkedin/auth/check")
def linkedin_auth_check(payload: SetupConfigUpdate) -> LinkedInAuthStatus:
    result = get_linkedin_auth_status(payload.linkedin.browser_profile_path)
    return LinkedInAuthStatus(**result)


@router.post("/api/linkedin/browser/terminate")
def linkedin_browser_terminate(payload: LinkedInBrowserTerminateRequest) -> LinkedInBrowserTerminateResponse:
    result = terminate_linkedin_browser(payload.pid)
    return LinkedInBrowserTerminateResponse(**result)


@router.get("/api/google/auth/status")
def google_auth_status():
    with get_db_session() as session:
        return get_google_auth_status(session)


@router.get("/api/google/auth/start")
def google_auth_start():
    with get_db_session() as session:
        try:
            authorization_url = start_google_oauth(session)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    return RedirectResponse(url=authorization_url)


@router.get("/api/google/callback", response_class=HTMLResponse)
def google_auth_callback(code: str, state: str):
    with get_db_session() as session:
        try:
            complete_google_oauth(session, code=code, state=state)
            config = load_setup_config(session)
        except Exception as error:
            logger.exception("Google OAuth callback failed")
            raise HTTPException(status_code=400, detail=f"Google OAuth callback failed: {error}") from error
    redirect_path = "/setup/?google=connected" if config.onboarding.wizard_completed else "/onboarding/?google=connected"
    return """
    <html><body>
      <script>
        window.location.href = 'REDIRECT_PATH';
      </script>
      <p>Google authentication completed. You can close this tab.</p>
    </body></html>
    """.replace("REDIRECT_PATH", redirect_path)


@router.post("/api/google/resources/validate")
def google_validate_resources(payload: SetupConfigUpdate) -> GoogleResourceValidationResponse:
    validation = validate_setup_config(payload)
    results: list[GoogleResourceValidationItem] = []
    resources = validation.normalized.google.resources.model_dump()

    connected = get_google_auth_status().connected
    if not connected:
        return GoogleResourceValidationResponse(connected=False, results=results)

    for field_name, resource_id in resources.items():
        if not resource_id:
            continue
        result = validate_drive_resource(resource_id)
        results.append(
            GoogleResourceValidationItem(
                field_name=field_name,
                resource_id=result["resource_id"],
                ok=result["ok"],
                mime_type=result["mime_type"],
                name=result["name"],
                url=result["url"],
                error=result["error"],
            )
        )

    return GoogleResourceValidationResponse(connected=True, results=results)


def _resolve_field_value(normalized, field_name: str):
    value = normalized
    for part in field_name.split("."):
        value = getattr(value, part)
    return value
