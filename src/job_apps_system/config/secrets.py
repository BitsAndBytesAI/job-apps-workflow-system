from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.config.models import SecretFieldStatus, SecretHelperStatus
from job_apps_system.config.settings import settings
from job_apps_system.db.models.settings import AppSetting
from job_apps_system.db.session import SessionLocal


SECRET_SETTINGS_PREFIX = "secret:"
SECRET_STORE_SERVICE = "ai.bitsandbytes.jobapps.secret.v1"
SECRET_BACKEND_ENV = "JOB_APPS_SECRET_BACKEND"
SECRET_HELPER_ENV = "JOB_APPS_SECRET_HELPER"
SECRET_PAYLOAD_FD_ENV = "JOB_APPS_SECRET_PAYLOAD_FD"
ALLOW_UNSIGNED_HELPER_ENV = "JOB_APPS_ALLOW_UNSIGNED_HELPER"
SECRET_PROTOCOL_VERSION = 1
PYTHON_NATIVE_BACKEND = "python_native"
NATIVE_HELPER_BACKEND = "native_helper"
PACKAGED_ENVS = {"packaged_debug", "packaged"}
KNOWN_SECRET_METADATA = {
    "openai_api_key": {
        "label": "Job Apps - OpenAI API Key",
        "description": "OpenAI API key for AI Job Agents.",
    },
    "anthropic_api_key": {
        "label": "Job Apps - Anthropic API Key",
        "description": "Anthropic API key for AI Job Agents.",
    },
    "anymailfinder_api_key": {
        "label": "Job Apps - Anymailfinder API Key",
        "description": "Anymailfinder API key for AI Job Agents.",
    },
    "google_oauth_token_json": {
        "label": "Job Apps - Google OAuth Token",
        "description": "Stored Google OAuth credentials for AI Job Agents.",
    },
}

_INJECTED_SECRET_CACHE: dict[str, str] | None = None


@dataclass
class NativeSecretHelperError(Exception):
    code: str
    message: str
    detail: Any = None

    def __str__(self) -> str:
        return self.message


def resolve_secret_backend() -> str:
    forced = (os.getenv(SECRET_BACKEND_ENV) or "").strip().lower()
    if forced in {PYTHON_NATIVE_BACKEND, NATIVE_HELPER_BACKEND}:
        return forced
    if settings.app_env in PACKAGED_ENVS:
        return NATIVE_HELPER_BACKEND
    return PYTHON_NATIVE_BACKEND


def known_secret_names() -> tuple[str, ...]:
    return tuple(KNOWN_SECRET_METADATA.keys())


def get_secret(secret_name: str, session: Session | None = None) -> str | None:
    if resolve_secret_backend() == PYTHON_NATIVE_BACKEND:
        return _with_session(session, lambda db: _sqlite_get_secret(db, secret_name))

    cache = _load_injected_secret_cache()
    cached = cache.get(secret_name)
    if cached:
        return cached

    try:
        response = _invoke_native_helper({"verb": "get", "secret_name": secret_name})
    except NativeSecretHelperError as exc:
        if exc.code != "missing_secret":
            return None
        legacy_value = _read_legacy_secret(secret_name, session=session)
        if not legacy_value:
            return None
        if set_secret(secret_name, legacy_value, session=session):
            _delete_legacy_secret(secret_name, session=session)
            cache[secret_name] = legacy_value
            return legacy_value
        return legacy_value

    secret_value = response.get("secret_value")
    if isinstance(secret_value, str) and secret_value:
        cache[secret_name] = secret_value
        return secret_value
    return None


def set_secret(secret_name: str, secret_value: str, session: Session | None = None) -> bool:
    if not secret_value:
        return False

    if resolve_secret_backend() == PYTHON_NATIVE_BACKEND:
        return _with_session(session, lambda db: _sqlite_set_secret(db, secret_name, secret_value), write=True)

    metadata = KNOWN_SECRET_METADATA.get(secret_name, {})
    try:
        _invoke_native_helper(
            {
                "verb": "put",
                "secret_name": secret_name,
                "secret_value": secret_value,
                "label": metadata.get("label"),
                "description": metadata.get("description"),
            }
        )
    except NativeSecretHelperError:
        return False

    _load_injected_secret_cache()[secret_name] = secret_value
    return True


def delete_secret(secret_name: str, session: Session | None = None) -> bool:
    if resolve_secret_backend() == PYTHON_NATIVE_BACKEND:
        return _with_session(session, lambda db: _sqlite_delete_secret(db, secret_name), write=True)

    deleted = False
    try:
        _invoke_native_helper({"verb": "delete", "secret_name": secret_name})
        deleted = True
    except NativeSecretHelperError as exc:
        if exc.code != "missing_secret":
            deleted = False

    _load_injected_secret_cache().pop(secret_name, None)
    if session is not None and _read_legacy_secret(secret_name, session=session):
        _delete_legacy_secret(secret_name, session=session)
        deleted = True
    return deleted


def has_secret(secret_name: str, session: Session | None = None) -> bool:
    return get_secret_status(secret_name, session=session).configured


def get_secret_status(secret_name: str, session: Session | None = None) -> SecretFieldStatus:
    timestamp = _utc_now()
    if resolve_secret_backend() == PYTHON_NATIVE_BACKEND:
        configured = _with_session(session, lambda db: _sqlite_get_secret(db, secret_name)) is not None
        if configured:
            return SecretFieldStatus(
                configured=True,
                status_code="configured",
                status_message="Key stored and ready.",
                last_validated_at=timestamp,
            )
        return SecretFieldStatus(
            configured=False,
            status_code="missing_secret",
            status_message="Not configured.",
            last_validated_at=timestamp,
        )

    cache = _load_injected_secret_cache()
    if cache.get(secret_name):
        return SecretFieldStatus(
            configured=True,
            status_code="configured",
            status_message="Key stored and ready.",
            last_validated_at=timestamp,
        )

    try:
        response = _invoke_native_helper({"verb": "get", "secret_name": secret_name})
    except NativeSecretHelperError as exc:
        if exc.code == "missing_secret":
            legacy_value = _read_legacy_secret(secret_name, session=session) if session is not None else None
            if legacy_value and set_secret(secret_name, legacy_value, session=session):
                _delete_legacy_secret(secret_name, session=session)
                cache[secret_name] = legacy_value
                return SecretFieldStatus(
                    configured=True,
                    status_code="configured",
                    status_message="Key stored and ready.",
                    last_validated_at=timestamp,
                )
            return SecretFieldStatus(
                configured=False,
                status_code="missing_secret",
                status_message="Not configured.",
                last_validated_at=timestamp,
            )
        return SecretFieldStatus(
            configured=False,
            status_code=exc.code,
            status_message=_secret_error_message(exc.code, exc.message),
            last_validated_at=timestamp,
        )

    secret_value = response.get("secret_value")
    configured = isinstance(secret_value, str) and bool(secret_value)
    if configured:
        cache[secret_name] = secret_value
    return SecretFieldStatus(
        configured=configured,
        status_code="configured" if configured else "missing_secret",
        status_message="Key stored and ready." if configured else "Not configured.",
        last_validated_at=timestamp,
    )


def get_secret_helper_status(session: Session | None = None) -> SecretHelperStatus:
    backend = resolve_secret_backend()
    if backend == PYTHON_NATIVE_BACKEND:
        return SecretHelperStatus(
            backend=backend,
            available=True,
            healthy=True,
            status_message="Using local secret storage in development mode.",
        )

    helper_path = resolve_native_helper_path()
    if helper_path is None:
        return SecretHelperStatus(
            backend=backend,
            available=False,
            healthy=False,
            last_error_code="helper_not_found",
            status_message="Secret helper is missing from the app bundle. Reinstall the app.",
        )

    try:
        response = _invoke_native_helper({"verb": "healthcheck"})
    except NativeSecretHelperError as exc:
        return SecretHelperStatus(
            backend=backend,
            available=True,
            healthy=False,
            last_error_code=exc.code,
            status_message=_secret_error_message(exc.code, exc.message),
        )

    probe_ok = bool(response.get("probe_round_trip_ok"))
    codesign_ok = bool(response.get("codesign_ok", True))
    entitlements_ok = bool(response.get("entitlements_ok", True))
    access_group_ok = bool(response.get("access_group_ok", True))
    healthy = probe_ok and codesign_ok and entitlements_ok and access_group_ok
    return SecretHelperStatus(
        backend=backend,
        available=True,
        healthy=healthy,
        helper_version=_as_optional_str(response.get("helper_version")),
        protocol_version=_as_optional_int(response.get("protocol_version")),
        last_error_code=None if healthy else "helper_runtime_failure",
        status_message=_as_optional_str(response.get("status_message"))
        or ("Keychain helper is healthy." if healthy else "Secret helper failed unexpectedly. Check logs."),
        codesign_ok=codesign_ok,
        entitlements_ok=entitlements_ok,
        access_group_ok=access_group_ok,
        probe_round_trip_ok=probe_ok,
    )


def migrate_legacy_secrets(session: Session | None = None) -> dict[str, bool]:
    results: dict[str, bool] = {}
    if resolve_secret_backend() != NATIVE_HELPER_BACKEND:
        return results

    for secret_name in known_secret_names():
        legacy_value = _read_legacy_secret(secret_name, session=session)
        if not legacy_value:
            continue
        migrated = set_secret(secret_name, legacy_value, session=session)
        if migrated:
            _delete_legacy_secret(secret_name, session=session)
        results[secret_name] = migrated
    return results


def resolve_native_helper_path() -> Path | None:
    explicit = (os.getenv(SECRET_HELPER_ENV) or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate.resolve()

    executable = Path(sys.executable).resolve()
    for parent in executable.parents:
        if parent.suffix != ".app":
            continue
        candidate = parent / "Contents" / "Helpers" / "JobAppsSecretHelper.app" / "Contents" / "MacOS" / "JobAppsSecretHelper"
        if candidate.exists():
            return candidate.resolve()
    return None


def _invoke_native_helper(payload: dict[str, Any]) -> dict[str, Any]:
    helper_path = resolve_native_helper_path()
    if helper_path is None:
        raise NativeSecretHelperError("helper_not_found", "Secret helper is missing from the app bundle.")

    request_body = json.dumps(payload)
    process = subprocess.run(
        [str(helper_path)],
        input=request_body,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    stdout = (process.stdout or "").strip()
    stderr = (process.stderr or "").strip()

    try:
        parsed = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise NativeSecretHelperError(
            "helper_runtime_failure",
            f"Secret helper returned invalid JSON: {stderr or stdout or exc}",
        ) from exc

    if process.returncode != 0 and not parsed:
        raise NativeSecretHelperError(
            "helper_runtime_failure",
            stderr or f"Secret helper exited with status {process.returncode}.",
        )

    if not parsed.get("ok", False):
        error = parsed.get("error") if isinstance(parsed, dict) else {}
        code = _as_optional_str((error or {}).get("code")) or "helper_runtime_failure"
        message = _as_optional_str((error or {}).get("message")) or "Secret helper failed unexpectedly."
        detail = (error or {}).get("detail")
        raise NativeSecretHelperError(code, message, detail)

    return parsed


def _load_injected_secret_cache() -> dict[str, str]:
    global _INJECTED_SECRET_CACHE
    if _INJECTED_SECRET_CACHE is not None:
        return _INJECTED_SECRET_CACHE

    _INJECTED_SECRET_CACHE = {}
    raw_fd = (os.getenv(SECRET_PAYLOAD_FD_ENV) or "").strip()
    if not raw_fd:
        return _INJECTED_SECRET_CACHE

    try:
        fd = int(raw_fd)
    except ValueError:
        return _INJECTED_SECRET_CACHE

    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            payload = handle.read()
    except OSError:
        return _INJECTED_SECRET_CACHE
    finally:
        os.environ.pop(SECRET_PAYLOAD_FD_ENV, None)

    if not payload.strip():
        return _INJECTED_SECRET_CACHE

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return _INJECTED_SECRET_CACHE

    if not isinstance(parsed, dict):
        return _INJECTED_SECRET_CACHE

    for secret_name, secret_value in parsed.items():
        if secret_name not in KNOWN_SECRET_METADATA:
            continue
        if isinstance(secret_value, str) and secret_value:
            _INJECTED_SECRET_CACHE[secret_name] = secret_value
    return _INJECTED_SECRET_CACHE


def _read_legacy_secret(secret_name: str, session: Session | None = None) -> str | None:
    sqlite_value = _with_session(session, lambda db: _sqlite_get_secret(db, secret_name))
    if sqlite_value:
        return sqlite_value
    return _keyring_get_secret(secret_name)


def _delete_legacy_secret(secret_name: str, session: Session | None = None) -> None:
    _with_session(session, lambda db: _sqlite_delete_secret(db, secret_name), write=True)
    _keyring_delete_secret(secret_name)


def _secret_setting_key(secret_name: str) -> str:
    return f"{SECRET_SETTINGS_PREFIX}{secret_name}"


def _sqlite_get_secret(session: Session, secret_name: str) -> str | None:
    record = session.scalar(select(AppSetting).where(AppSetting.key == _secret_setting_key(secret_name)))
    if record is None:
        return None
    try:
        payload = json.loads(record.value_json)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        value = payload.get("value")
        return value if isinstance(value, str) and value else None
    if isinstance(payload, str):
        return payload or None
    return None


def _sqlite_set_secret(session: Session, secret_name: str, secret_value: str) -> bool:
    record = session.scalar(select(AppSetting).where(AppSetting.key == _secret_setting_key(secret_name)))
    if record is None:
        record = AppSetting(key=_secret_setting_key(secret_name), value_json="{}", updated_at=datetime.now(timezone.utc))
        session.add(record)
    record.value_json = json.dumps({"value": secret_value}, indent=2)
    record.updated_at = datetime.now(timezone.utc)
    session.flush()
    return True


def _sqlite_delete_secret(session: Session, secret_name: str) -> bool:
    record = session.scalar(select(AppSetting).where(AppSetting.key == _secret_setting_key(secret_name)))
    if record is None:
        return False
    session.delete(record)
    session.flush()
    return True


def _keyring_get_secret(secret_name: str) -> str | None:
    try:
        import keyring  # type: ignore
    except Exception:
        return None

    try:
        value = keyring.get_password(SECRET_STORE_SERVICE, secret_name)
    except Exception:
        return None
    return value or None


def _keyring_delete_secret(secret_name: str) -> None:
    try:
        import keyring  # type: ignore
        from keyring.errors import PasswordDeleteError  # type: ignore
    except Exception:
        return

    try:
        keyring.delete_password(SECRET_STORE_SERVICE, secret_name)
    except PasswordDeleteError:
        return
    except Exception:
        return


def _secret_error_message(code: str, fallback: str) -> str:
    return {
        "missing_secret": "Not configured.",
        "helper_not_found": "Secret helper is missing from the app bundle. Reinstall the app.",
        "schema_invalid": "Stored secret is unreadable. Re-enter it.",
        "schema_too_new": "Stored secret is unreadable. Re-enter it.",
        "codesign_invalid": "Secret helper signature is invalid. Reinstall the app.",
        "entitlement_missing": "App secret access is misconfigured. Reinstall or contact support.",
        "access_group_misconfigured": "App secret access is misconfigured. Reinstall or contact support.",
        "keychain_unavailable": "Keychain unavailable in current session. Log in again and retry.",
        "helper_runtime_failure": "Secret helper failed unexpectedly. Check logs.",
        "unknown_secret_name": "Secret helper rejected an unknown secret.",
    }.get(code, fallback or "Secret helper failed unexpectedly. Check logs.")


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _with_session(session: Session | None, operation, write: bool = False):
    if session is not None:
        return operation(session)

    with SessionLocal() as db:
        result = operation(db)
        if write:
            db.commit()
        return result
