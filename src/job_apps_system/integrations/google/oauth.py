from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.orm import Session

from job_apps_system.config.models import GoogleAuthStatus
from job_apps_system.config.secrets import delete_secret, get_secret, set_secret
from job_apps_system.config.settings import settings
from job_apps_system.services.setup_config import (
    GOOGLE_OAUTH_PENDING_STATE_KEY,
    delete_json_setting,
    get_json_setting,
    set_json_setting,
)


GOOGLE_OAUTH_TOKEN_SECRET = "google_oauth_token_json"
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/gmail.send",
]
logger = logging.getLogger(__name__)


def get_google_auth_status(session: Session | None = None) -> GoogleAuthStatus:
    return GoogleAuthStatus(
        connected=get_google_credentials(session=session) is not None,
        client_configured=_get_client_config() is not None,
        redirect_uri=settings.resolved_google_redirect_uri,
        scopes=GOOGLE_SCOPES,
    )


def start_google_oauth(session: Session) -> str:
    client_config = _get_client_config()
    if client_config is None:
        raise ValueError("Google OAuth client configuration is not available.")

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=settings.resolved_google_redirect_uri,
        autogenerate_code_verifier=True,
    )
    state = secrets.token_urlsafe(32)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    if not flow.code_verifier:
        raise ValueError("Google OAuth PKCE verifier was not generated.")
    logger.info(
        "Starting Google OAuth flow: state_prefix=%s verifier_length=%s redirect_uri=%s",
        state[:8],
        len(flow.code_verifier),
        settings.resolved_google_redirect_uri,
    )
    set_json_setting(
        session,
        GOOGLE_OAUTH_PENDING_STATE_KEY,
        {"state": state, "code_verifier": flow.code_verifier},
    )
    return authorization_url


def complete_google_oauth(session: Session, code: str, state: str) -> None:
    pending = get_json_setting(session, GOOGLE_OAUTH_PENDING_STATE_KEY, {})
    expected_state = (pending or {}).get("state")
    code_verifier = (pending or {}).get("code_verifier")
    logger.info(
        "Completing Google OAuth callback: state_prefix=%s pending_state_prefix=%s has_verifier=%s",
        state[:8],
        expected_state[:8] if isinstance(expected_state, str) else None,
        bool(code_verifier),
    )
    if not expected_state or expected_state != state:
        raise ValueError("Google OAuth state did not match the pending login request.")
    if not code_verifier:
        raise ValueError("Google OAuth PKCE verifier is missing from the pending login request.")

    client_config = _get_client_config()
    if client_config is None:
        raise ValueError("Google OAuth client configuration is not available.")

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=GOOGLE_SCOPES,
        redirect_uri=settings.resolved_google_redirect_uri,
        state=state,
    )
    flow.code_verifier = code_verifier
    logger.info("Fetching Google OAuth token for state_prefix=%s", state[:8])
    flow.fetch_token(code=code)
    logger.info(
        "Google OAuth token fetched: has_refresh_token=%s granted_scopes=%s",
        bool(flow.credentials.refresh_token),
        sorted(flow.credentials.scopes or []),
    )
    if not set_secret(GOOGLE_OAUTH_TOKEN_SECRET, flow.credentials.to_json(), session=session):
        raise ValueError("Unable to store Google OAuth tokens in the local secret store.")
    logger.info("Stored Google OAuth token in local secret store for state_prefix=%s", state[:8])
    delete_json_setting(session, GOOGLE_OAUTH_PENDING_STATE_KEY)


def get_google_credentials(session: Session | None = None) -> Credentials | None:
    token_json = get_secret(GOOGLE_OAUTH_TOKEN_SECRET, session=session)
    if token_json is None:
        logger.info("Google OAuth credentials not found in local secret store")
        return None

    token_data = json.loads(token_json)
    if not isinstance(token_data, dict):
        raise ValueError('Stored Google OAuth token payload is invalid.')
    credentials = Credentials.from_authorized_user_info(token_data, GOOGLE_SCOPES)
    if credentials.expired and credentials.refresh_token:
        logger.info("Refreshing expired Google OAuth credentials")
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            logger.warning("Google OAuth refresh failed; clearing stored token: %s", exc)
            delete_secret(GOOGLE_OAUTH_TOKEN_SECRET, session=session)
            return None
        set_secret(GOOGLE_OAUTH_TOKEN_SECRET, credentials.to_json(), session=session)
        logger.info("Stored refreshed Google OAuth credentials in local secret store")
    return credentials


def _get_client_config() -> dict[str, Any] | None:
    parsed: dict[str, Any] | None = None

    if settings.google_oauth_client_config_path:
        path = Path(settings.google_oauth_client_config_path).expanduser()
        if path.exists():
            try:
                parsed = json.loads(path.read_text())
            except json.JSONDecodeError:
                parsed = None

    if parsed is None and settings.google_oauth_client_config_json:
        try:
            parsed = json.loads(settings.google_oauth_client_config_json)
        except json.JSONDecodeError:
            parsed = None

    if parsed is None:
        return None

    if "installed" in parsed or "web" in parsed:
        return parsed
    return None
