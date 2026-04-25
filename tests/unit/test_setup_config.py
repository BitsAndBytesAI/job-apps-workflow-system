from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.config.models import GoogleAuthStatus, SetupConfig
from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.integrations.google import oauth as google_oauth
from job_apps_system.services.setup_config import (
    GOOGLE_OAUTH_PENDING_STATE_KEY,
    delete_json_setting,
    get_json_setting,
    set_json_setting,
    with_live_connection_status,
)


class SetupConfigStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = session_factory()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_with_live_connection_status_hydrates_google_and_linkedin_flags(self) -> None:
        config = SetupConfig()

        with (
            patch(
                "job_apps_system.integrations.linkedin.auth.get_linkedin_auth_status",
                return_value={"authenticated": True},
            ),
            patch(
                "job_apps_system.integrations.google.oauth.get_google_auth_status",
                return_value=GoogleAuthStatus(
                    connected=True,
                    client_configured=True,
                    redirect_uri="http://127.0.0.1/callback",
                    scopes=[],
                ),
            ),
        ):
            hydrated = with_live_connection_status(config, self.session)

        self.assertFalse(config.linkedin.authenticated)
        self.assertFalse(config.google.connected)
        self.assertTrue(hydrated.linkedin.authenticated)
        self.assertTrue(hydrated.google.connected)

    def test_with_live_connection_status_falls_back_to_false_when_status_checks_fail(self) -> None:
        config = SetupConfig()
        config.linkedin.authenticated = True
        config.google.connected = True

        with (
            patch(
                "job_apps_system.integrations.linkedin.auth.get_linkedin_auth_status",
                side_effect=RuntimeError("linkedin unavailable"),
            ),
            patch(
                "job_apps_system.integrations.google.oauth.get_google_auth_status",
                side_effect=RuntimeError("google unavailable"),
            ),
        ):
            hydrated = with_live_connection_status(config, self.session)

        self.assertFalse(hydrated.linkedin.authenticated)
        self.assertFalse(hydrated.google.connected)


class GoogleOAuthPendingStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = session_factory()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_complete_google_oauth_deletes_pending_state_after_storing_token(self) -> None:
        set_json_setting(
            self.session,
            GOOGLE_OAUTH_PENDING_STATE_KEY,
            {"state": "expected-state", "code_verifier": "expected-verifier"},
        )
        fake_flow = SimpleNamespace(
            code_verifier=None,
            credentials=SimpleNamespace(
                refresh_token="refresh-token",
                scopes=list(google_oauth.GOOGLE_SCOPES),
                to_json=lambda: '{"refresh_token":"refresh-token"}',
            ),
            fetch_token=Mock(),
        )

        with (
            patch.object(google_oauth, "_get_client_config", return_value={"installed": {}}),
            patch.object(google_oauth.Flow, "from_client_config", return_value=fake_flow),
            patch.object(google_oauth, "set_secret", return_value=True) as set_secret,
        ):
            google_oauth.complete_google_oauth(
                self.session,
                code="auth-code",
                state="expected-state",
            )

        self.assertEqual(fake_flow.code_verifier, "expected-verifier")
        fake_flow.fetch_token.assert_called_once_with(code="auth-code")
        set_secret.assert_called_once_with(
            google_oauth.GOOGLE_OAUTH_TOKEN_SECRET,
            '{"refresh_token":"refresh-token"}',
            session=self.session,
        )
        self.assertIsNone(get_json_setting(self.session, GOOGLE_OAUTH_PENDING_STATE_KEY, None))

    def test_delete_json_setting_is_safe_when_key_is_missing(self) -> None:
        delete_json_setting(self.session, "missing-key")
        self.assertIsNone(get_json_setting(self.session, "missing-key", None))


if __name__ == "__main__":
    unittest.main()
