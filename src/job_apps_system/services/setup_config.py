from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.config.models import (
    FieldValidationResponse,
    GoogleManagedResourcesConfig,
    PersistedFieldValidation,
    SetupConfig,
    SetupConfigUpdate,
    SetupValidationResponse,
)
from job_apps_system.config.resource_ids import normalize_google_resource_id
from job_apps_system.config.secrets import has_secret, set_secret
from job_apps_system.db.models.settings import AppSetting
from job_apps_system.integrations.linkedin.browser import (
    DEFAULT_FIREFOX_LINKEDIN_PROFILE,
    DEFAULT_BUNDLED_LINKEDIN_PROFILE,
    LEGACY_BUNDLED_LINKEDIN_PROFILES,
    LEGACY_LINKEDIN_PROFILE,
)


SETUP_CONFIG_KEY = "setup_config"
GOOGLE_OAUTH_PENDING_STATE_KEY = "google_oauth_pending_state"
FIELD_VALIDATIONS_KEY = "setup_field_validations"
SECRET_FIELD_NAMES = {
    "openai_api_key": "openai_api_key",
    "anthropic_api_key": "anthropic_api_key",
    "anymailfinder_api_key": "anymailfinder_api_key",
}


def load_setup_config(session: Session) -> SetupConfig:
    record = session.scalar(select(AppSetting).where(AppSetting.key == SETUP_CONFIG_KEY))
    if record is None:
        config = SetupConfig()
    else:
        config = SetupConfig.model_validate(json.loads(record.value_json))

    if config.linkedin.browser_profile_path in LEGACY_BUNDLED_LINKEDIN_PROFILES:
        config.linkedin.browser_profile_path = DEFAULT_FIREFOX_LINKEDIN_PROFILE

    config.google.managed_resources = load_google_managed_resources(session)
    config.secrets.openai_api_key_configured = has_secret("openai_api_key", session=session)
    config.secrets.anthropic_api_key_configured = has_secret("anthropic_api_key", session=session)
    config.secrets.anymailfinder_api_key_configured = has_secret("anymailfinder_api_key", session=session)
    config.field_validations = load_field_validations(session)
    return config


def validate_setup_config(payload: SetupConfigUpdate) -> SetupValidationResponse:
    normalized = SetupConfig(
        google=payload.google,
        linkedin=payload.linkedin,
        models=payload.models,
        onboarding=payload.onboarding,
        project_resume=payload.project_resume,
        applicant=payload.applicant,
        app=payload.app,
    )
    normalized.google.resources = _normalize_google_resources(normalized.google.resources)
    normalized.app.project_id = _normalize_project_id(
        normalized.app.project_id,
        normalized.app.project_name,
        normalized.app.job_role,
    )
    normalized.app.selected_job_sites = _normalize_selected_job_sites(normalized.app.selected_job_sites)
    normalized.onboarding.wizard_current_step = _normalize_wizard_step(normalized.onboarding.wizard_current_step)
    if normalized.linkedin.browser_profile_path in LEGACY_BUNDLED_LINKEDIN_PROFILES:
        normalized.linkedin.browser_profile_path = DEFAULT_FIREFOX_LINKEDIN_PROFILE

    errors: list[str] = []
    for url in normalized.linkedin.search_urls:
        if "linkedin.com/jobs" not in url:
            errors.append(f"LinkedIn search URL is not a jobs URL: {url}")

    normalized.secrets.openai_api_key_configured = bool(payload.secrets.openai_api_key) or has_secret("openai_api_key")
    normalized.secrets.anthropic_api_key_configured = bool(payload.secrets.anthropic_api_key) or has_secret("anthropic_api_key")
    normalized.secrets.anymailfinder_api_key_configured = bool(payload.secrets.anymailfinder_api_key) or has_secret("anymailfinder_api_key")
    return SetupValidationResponse(normalized=normalized, errors=errors)


def save_setup_config(session: Session, payload: SetupConfigUpdate) -> SetupConfig:
    raw_google_resources = payload.google.resources.model_copy(deep=True)
    validation = validate_setup_config(payload)
    config = validation.normalized
    persisted_config = config.model_copy(deep=True)
    persisted_config.google.resources = raw_google_resources

    record = session.scalar(select(AppSetting).where(AppSetting.key == SETUP_CONFIG_KEY))
    if record is None:
        record = AppSetting(key=SETUP_CONFIG_KEY, value_json="{}", updated_at=datetime.now(timezone.utc))
        session.add(record)

    record.value_json = json.dumps(
        persisted_config.model_dump(
            mode="json",
            exclude={
                "secrets": True,
                "field_validations": True,
                "google": {"managed_resources"},
            },
        ),
        indent=2,
    )
    record.updated_at = datetime.now(timezone.utc)

    if payload.secrets.openai_api_key:
        set_secret(SECRET_FIELD_NAMES["openai_api_key"], payload.secrets.openai_api_key, session=session)
    if payload.secrets.anthropic_api_key:
        set_secret(SECRET_FIELD_NAMES["anthropic_api_key"], payload.secrets.anthropic_api_key, session=session)
    if payload.secrets.anymailfinder_api_key:
        set_secret(SECRET_FIELD_NAMES["anymailfinder_api_key"], payload.secrets.anymailfinder_api_key, session=session)

    prune_field_validations(session, config)
    session.flush()
    return load_setup_config(session)


def build_setup_update(config: SetupConfig) -> SetupConfigUpdate:
    return SetupConfigUpdate(
        google=config.google.model_copy(deep=True),
        linkedin=config.linkedin.model_copy(deep=True),
        models=config.models.model_copy(deep=True),
        onboarding=config.onboarding.model_copy(deep=True),
        project_resume=config.project_resume.model_copy(deep=True),
        applicant=config.applicant.model_copy(deep=True),
        app=config.app.model_copy(deep=True),
    )


def get_json_setting(session: Session, key: str, default=None):
    record = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if record is None:
        return default
    return json.loads(record.value_json)


def set_json_setting(session: Session, key: str, value) -> None:
    record = session.scalar(select(AppSetting).where(AppSetting.key == key))
    if record is None:
        record = AppSetting(key=key, value_json="{}", updated_at=datetime.now(timezone.utc))
        session.add(record)
    record.value_json = json.dumps(value, indent=2)
    record.updated_at = datetime.now(timezone.utc)
    session.flush()


def load_google_managed_resources(session: Session) -> GoogleManagedResourcesConfig:
    project_id = _load_current_project_id(session)
    payload = get_json_setting(session, _google_managed_resources_key(project_id), {})
    if not isinstance(payload, dict):
        return GoogleManagedResourcesConfig()
    try:
        return GoogleManagedResourcesConfig.model_validate(payload)
    except Exception:
        return GoogleManagedResourcesConfig()


def save_google_managed_resources(session: Session, resources: GoogleManagedResourcesConfig) -> GoogleManagedResourcesConfig:
    project_id = _load_current_project_id(session)
    set_json_setting(
        session,
        _google_managed_resources_key(project_id),
        resources.model_dump(mode="json"),
    )
    return load_google_managed_resources(session)


def load_field_validations(session: Session) -> dict[str, PersistedFieldValidation]:
    payload = get_json_setting(session, FIELD_VALIDATIONS_KEY, {})
    if not isinstance(payload, dict):
        return {}

    results: dict[str, PersistedFieldValidation] = {}
    for field_name, value in payload.items():
        try:
            results[field_name] = PersistedFieldValidation.model_validate(value)
        except Exception:
            continue
    return results


def save_field_validation(session: Session, validation: FieldValidationResponse) -> None:
    existing = get_json_setting(session, FIELD_VALIDATIONS_KEY, {})
    if not isinstance(existing, dict):
        existing = {}
    existing[validation.field_name] = PersistedFieldValidation(
        ok=validation.ok,
        message=validation.message,
        level=validation.level,
        normalized_value=validation.normalized_value,
        updated_at=validation.updated_at or datetime.now(timezone.utc).isoformat(),
    ).model_dump(mode="json")
    set_json_setting(session, FIELD_VALIDATIONS_KEY, existing)


def prune_field_validations(session: Session, config: SetupConfig) -> None:
    existing = get_json_setting(session, FIELD_VALIDATIONS_KEY, {})
    if not isinstance(existing, dict):
        return

    pruned: dict[str, dict] = {}
    for field_name, value in existing.items():
        if field_name.startswith("secrets."):
            continue
        normalized_value = value.get("normalized_value") if isinstance(value, dict) else None
        current_value = _resolve_field_value(config, field_name)
        if current_value is None:
            continue
        if isinstance(current_value, list):
            if normalized_value == len(current_value):
                pruned[field_name] = value
            continue
        if current_value == normalized_value:
            pruned[field_name] = value

    set_json_setting(session, FIELD_VALIDATIONS_KEY, pruned)


def _normalize_google_resources(resources):
    normalized = resources.model_copy(deep=True)
    for field_name, value in normalized:
        if isinstance(value, str) and value.strip():
            setattr(normalized, field_name, normalize_google_resource_id(value))
    return normalized


def _resolve_field_value(config: SetupConfig, field_name: str):
    value = config
    for part in field_name.split("."):
        try:
            value = getattr(value, part)
        except AttributeError:
            return None
    return value


def _normalize_project_id(
    value: str | None,
    project_name: str | None = None,
    job_role: str | None = None,
) -> str:
    candidate = (value or "").strip().lower()
    if not candidate and project_name:
        candidate = project_name.strip().lower()
    if not candidate and job_role:
        candidate = job_role.strip().lower()

    normalized = []
    last_was_separator = False
    for char in candidate:
        if char.isalnum():
            normalized.append(char)
            last_was_separator = False
        elif char in {"-", "_", " "} and not last_was_separator:
            normalized.append("-")
            last_was_separator = True

    result = "".join(normalized).strip("-")
    return result or "default-project"


def _google_managed_resources_key(project_id: str) -> str:
    return f"google_managed_resources:{project_id}"


def _normalize_selected_job_sites(job_sites: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in job_sites:
        candidate = (value or "").strip().lower()
        if not candidate or candidate not in {"linkedin"} or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _normalize_wizard_step(value: str | None) -> str:
    allowed_steps = {
        "project",
        "resume",
        "job-sites",
        "models",
        "anymailfinder",
        "score-threshold",
        "applicant",
        "google",
        "complete",
    }
    candidate = (value or "").strip().lower()
    return candidate if candidate in allowed_steps else "project"


def _load_current_project_id(session: Session) -> str:
    record = session.scalar(select(AppSetting).where(AppSetting.key == SETUP_CONFIG_KEY))
    if record is None:
        return _normalize_project_id(SetupConfig().app.project_id, SetupConfig().app.project_name, SetupConfig().app.job_role)

    try:
        payload = json.loads(record.value_json)
    except json.JSONDecodeError:
        return _normalize_project_id(SetupConfig().app.project_id, SetupConfig().app.project_name, SetupConfig().app.job_role)

    try:
        config = SetupConfig.model_validate(payload if isinstance(payload, dict) else {})
    except Exception:
        return _normalize_project_id(SetupConfig().app.project_id, SetupConfig().app.project_name, SetupConfig().app.job_role)
    return _normalize_project_id(config.app.project_id, config.app.project_name, config.app.job_role)
