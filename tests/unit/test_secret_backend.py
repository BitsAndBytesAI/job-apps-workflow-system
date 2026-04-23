from __future__ import annotations

import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from job_apps_system.config import secrets as secrets_module


class SecretBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        temp_path = Path(self.tempdir.name)
        self.store_path = temp_path / "helper-store.json"
        self.helper_path = temp_path / "fake-helper.py"
        self.helper_path.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                store_path = Path(os.environ["JOB_APPS_TEST_HELPER_STORE"])
                if store_path.exists():
                    payload = json.loads(store_path.read_text())
                else:
                    payload = {}

                request = json.loads(sys.stdin.read() or "{}")
                verb = request.get("verb")
                response = {"ok": True, "protocol_version": 1, "helper_version": "test-helper"}

                if verb == "put":
                    payload[request["secret_name"]] = request["secret_value"]
                    store_path.write_text(json.dumps(payload))
                    response["status_message"] = "Key stored and ready."
                elif verb == "get":
                    secret_name = request["secret_name"]
                    if secret_name not in payload:
                        response = {
                            "ok": False,
                            "protocol_version": 1,
                            "error": {"code": "missing_secret", "message": "Secret not found.", "detail": None},
                        }
                    else:
                        response["secret_name"] = secret_name
                        response["secret_value"] = payload[secret_name]
                elif verb == "delete":
                    payload.pop(request["secret_name"], None)
                    store_path.write_text(json.dumps(payload))
                    response["status_message"] = "Secret deleted."
                elif verb == "get-batch":
                    response["secrets"] = {
                        name: value for name, value in payload.items() if name in set(request.get("secret_names", []))
                    }
                elif verb == "healthcheck":
                    response.update({
                        "status_message": "Keychain helper is healthy.",
                        "codesign_ok": True,
                        "entitlements_ok": True,
                        "access_group_ok": True,
                        "probe_round_trip_ok": True,
                    })
                else:
                    response = {
                        "ok": False,
                        "protocol_version": 1,
                        "error": {"code": "schema_invalid", "message": "Unsupported verb.", "detail": verb},
                    }

                print(json.dumps(response))
                sys.exit(0 if response.get("ok") else 1)
                """
            )
        )
        self.helper_path.chmod(self.helper_path.stat().st_mode | stat.S_IXUSR)

        self.original_app_env = secrets_module.settings.app_env
        self.original_cache = secrets_module._INJECTED_SECRET_CACHE
        secrets_module.settings.app_env = "packaged_debug"
        secrets_module._INJECTED_SECRET_CACHE = None
        os.environ[secrets_module.SECRET_BACKEND_ENV] = secrets_module.NATIVE_HELPER_BACKEND
        os.environ[secrets_module.SECRET_HELPER_ENV] = str(self.helper_path)
        os.environ["JOB_APPS_TEST_HELPER_STORE"] = str(self.store_path)

    def tearDown(self) -> None:
        secrets_module.settings.app_env = self.original_app_env
        secrets_module._INJECTED_SECRET_CACHE = self.original_cache
        os.environ.pop(secrets_module.SECRET_BACKEND_ENV, None)
        os.environ.pop(secrets_module.SECRET_HELPER_ENV, None)
        os.environ.pop("JOB_APPS_TEST_HELPER_STORE", None)
        self.tempdir.cleanup()

    def test_native_helper_backend_round_trips_and_reports_status(self) -> None:
        self.assertTrue(secrets_module.set_secret("openai_api_key", "sk-test"))
        self.assertEqual(secrets_module.get_secret("openai_api_key"), "sk-test")

        secret_status = secrets_module.get_secret_status("openai_api_key")
        self.assertTrue(secret_status.configured)
        self.assertEqual(secret_status.status_code, "configured")

        helper_status = secrets_module.get_secret_helper_status()
        self.assertTrue(helper_status.healthy)
        self.assertEqual(helper_status.backend, "native_helper")

        self.assertTrue(secrets_module.delete_secret("openai_api_key"))
        deleted_status = secrets_module.get_secret_status("openai_api_key")
        self.assertFalse(deleted_status.configured)
        self.assertEqual(deleted_status.status_code, "missing_secret")


if __name__ == "__main__":
    unittest.main()
