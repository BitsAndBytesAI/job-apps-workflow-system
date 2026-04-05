from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.db.models.settings import AppSetting
from job_apps_system.db.session import SessionLocal


SECRET_SETTINGS_PREFIX = "secret:"


def get_secret(secret_name: str, session: Session | None = None) -> str | None:
    return _with_session(session, lambda db: _get_secret(db, secret_name))


def set_secret(secret_name: str, secret_value: str, session: Session | None = None) -> bool:
    return _with_session(session, lambda db: _set_secret(db, secret_name, secret_value), write=True)


def delete_secret(secret_name: str, session: Session | None = None) -> bool:
    return _with_session(session, lambda db: _delete_secret(db, secret_name), write=True)


def has_secret(secret_name: str, session: Session | None = None) -> bool:
    return get_secret(secret_name, session=session) is not None


def _secret_setting_key(secret_name: str) -> str:
    return f"{SECRET_SETTINGS_PREFIX}{secret_name}"


def _get_secret(session: Session, secret_name: str) -> str | None:
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


def _set_secret(session: Session, secret_name: str, secret_value: str) -> bool:
    record = session.scalar(select(AppSetting).where(AppSetting.key == _secret_setting_key(secret_name)))
    if record is None:
        record = AppSetting(key=_secret_setting_key(secret_name), value_json='{}', updated_at=datetime.now(timezone.utc))
        session.add(record)
    record.value_json = json.dumps({"value": secret_value}, indent=2)
    record.updated_at = datetime.now(timezone.utc)
    session.flush()
    return True


def _delete_secret(session: Session, secret_name: str) -> bool:
    record = session.scalar(select(AppSetting).where(AppSetting.key == _secret_setting_key(secret_name)))
    if record is None:
        return False
    session.delete(record)
    session.flush()
    return True


def _with_session(session: Session | None, operation, write: bool = False):
    if session is not None:
        return operation(session)

    with SessionLocal() as db:
        result = operation(db)
        if write:
            db.commit()
        return result
