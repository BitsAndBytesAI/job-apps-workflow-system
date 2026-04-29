from __future__ import annotations

import io
import unittest
from urllib import error
from unittest.mock import patch

from job_apps_system.integrations.anymailfinder.client import (
    AnymailfinderError,
    find_decision_maker_email,
    infer_decision_maker_category,
)


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class AnymailfinderClientTests(unittest.TestCase):
    def test_infer_decision_maker_category_detects_engineering_roles(self) -> None:
        category = infer_decision_maker_category("Senior Software Engineer", "Build backend APIs")
        self.assertEqual(category, "engineering")

    def test_find_decision_maker_email_uses_authorization_header_and_parses_payload(self) -> None:
        with patch(
            "job_apps_system.integrations.anymailfinder.client.request.urlopen",
            return_value=_FakeResponse(
                '{"decision_maker_category":"ceo","email":"ceo@example.com","email_status":"valid","person_full_name":"Jane Doe","person_job_title":"CEO","person_linkedin_url":"https://linkedin.com/in/jane","valid_email":"ceo@example.com"}'
            ),
        ) as mocked_urlopen:
            result = find_decision_maker_email(
                "live-api-key",
                domain="example.com",
                company_name="Example",
                decision_maker_category="ceo",
            )

        api_request = mocked_urlopen.call_args.args[0]
        self.assertEqual(api_request.headers["Authorization"], "live-api-key")
        self.assertEqual(result.best_email, "ceo@example.com")
        self.assertEqual(result.person_full_name, "Jane Doe")
        self.assertEqual(result.person_job_title, "CEO")

    def test_find_decision_maker_email_maps_unauthorized_errors_to_setup_message(self) -> None:
        http_error = error.HTTPError(
            "https://api.anymailfinder.com/v5.1/find-email/decision-maker",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"bad api key"}'),
        )
        with patch(
            "job_apps_system.integrations.anymailfinder.client.request.urlopen",
            side_effect=http_error,
        ):
            with self.assertRaises(AnymailfinderError) as raised:
                find_decision_maker_email(
                    "bad-key",
                    company_name="Example",
                    decision_maker_category="ceo",
                )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("Update it in Setup", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
