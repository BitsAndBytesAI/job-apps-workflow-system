from __future__ import annotations

import json
import logging
import re
import secrets as py_secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from job_apps_system.config.secrets import (
    APPLY_SITE_CREDENTIAL_SECRET_PREFIX,
    NATIVE_HELPER_BACKEND,
    get_secret,
    resolve_secret_backend,
    set_secret,
)
from job_apps_system.config.settings import settings
from job_apps_system.runtime.paths import resolve_runtime_path


logger = logging.getLogger(__name__)

APPLY_PROFILE_ROOT = "browser-profiles/apply"
DEFAULT_APPLY_PROFILE_KEY = "generic"


@dataclass(frozen=True)
class ApplySiteCredential:
    site_key: str
    email: str
    password: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ApplySiteSession:
    site_key: str
    profile_path: Path
    credential: ApplySiteCredential | None = None


def site_key_for_url(url: str | None, ats_type: str | None = None) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower()
    if not host:
        return ats_type or DEFAULT_APPLY_PROFILE_KEY
    host = host.removeprefix("www.")

    if "linkedin.com" in host:
        return "linkedin"
    if host == "dice.com" or host.endswith(".dice.com") or "appcast.io" in host:
        return "dice"
    if "oraclecloud.com" in host:
        return "oracle-cloud"
    if "workdayjobs.com" in host or "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        return "workday"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "ashbyhq.com" in host:
        return "ashby"
    if "icims.com" in host:
        return "icims"
    if ats_type and ats_type != "unknown":
        return _safe_site_key(ats_type)
    return _safe_site_key(host)


def resolve_apply_profile_path(site_key: str, *, linkedin_profile_path: str | None = None) -> Path:
    normalized_key = _safe_site_key(site_key)
    if normalized_key == "linkedin" and linkedin_profile_path:
        return resolve_runtime_path(linkedin_profile_path, app_data_dir=settings.resolved_app_data_dir)
    return resolve_runtime_path(
        f"{APPLY_PROFILE_ROOT}/{normalized_key}",
        app_data_dir=settings.resolved_app_data_dir,
    )


def build_apply_site_session(
    *,
    url: str | None,
    ats_type: str | None,
    applicant_email: str | None,
    linkedin_profile_path: str | None = None,
    session: Session | None = None,
) -> ApplySiteSession:
    site_key = site_key_for_url(url, ats_type)
    profile_path = resolve_apply_profile_path(site_key, linkedin_profile_path=linkedin_profile_path)
    credential = get_or_create_apply_site_credential(site_key, applicant_email, session=session)
    return ApplySiteSession(site_key=site_key, profile_path=profile_path, credential=credential)


def get_or_create_apply_site_credential(
    site_key: str,
    applicant_email: str | None,
    *,
    session: Session | None = None,
) -> ApplySiteCredential | None:
    normalized_key = _safe_site_key(site_key)
    email = (applicant_email or "").strip()
    if not email:
        return None
    if resolve_secret_backend() != NATIVE_HELPER_BACKEND:
        logger.warning(
            "Apply-site credential generation skipped because native keychain helper is not active. site_key=%s",
            normalized_key,
        )
        return None

    existing = get_apply_site_credential(normalized_key, session=session)
    if existing and existing.email.lower() == email.lower():
        return existing

    now = datetime.now(timezone.utc).isoformat()
    credential = ApplySiteCredential(
        site_key=normalized_key,
        email=email,
        password=_generate_site_password(),
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    payload = json.dumps(
        {
            "site_key": credential.site_key,
            "email": credential.email,
            "password": credential.password,
            "created_at": credential.created_at,
            "updated_at": credential.updated_at,
        },
        separators=(",", ":"),
    )
    if not set_secret(_credential_secret_name(normalized_key), payload, session=session):
        logger.warning("Unable to store apply-site credential in native keychain helper. site_key=%s", normalized_key)
        return None
    return credential


def get_apply_site_credential(site_key: str, *, session: Session | None = None) -> ApplySiteCredential | None:
    if resolve_secret_backend() != NATIVE_HELPER_BACKEND:
        return None
    raw = get_secret(_credential_secret_name(site_key), session=session)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    if not email or not password:
        return None
    normalized_key = _safe_site_key(str(payload.get("site_key") or site_key))
    return ApplySiteCredential(
        site_key=normalized_key,
        email=email,
        password=password,
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )


def _credential_secret_name(site_key: str) -> str:
    return f"{APPLY_SITE_CREDENTIAL_SECRET_PREFIX}{_safe_site_key(site_key)}"


def _safe_site_key(value: str | None) -> str:
    cleaned = re.sub(r"[^a-z0-9.-]+", "-", (value or "").strip().lower())
    cleaned = cleaned.strip(".-")
    return cleaned or DEFAULT_APPLY_PROFILE_KEY


def _generate_site_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        password = "".join(py_secrets.choice(alphabet) for _ in range(length))
        if (
            any(char.islower() for char in password)
            and any(char.isupper() for char in password)
            and any(char.isdigit() for char in password)
            and any(char in "!@#$%^&*()-_=+" for char in password)
        ):
            return password
