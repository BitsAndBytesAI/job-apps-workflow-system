from __future__ import annotations

import unittest
from pathlib import Path

from job_apps_system.runtime.paths import (
    default_app_data_dir,
    resolve_database_url,
    resolve_runtime_path,
    sqlite_url_for_path,
)


class RuntimePathTests(unittest.TestCase):
    def test_default_app_data_dir_uses_application_support_in_development(self) -> None:
        resolved = default_app_data_dir(app_env="development")
        expected_suffix = Path("Library/Application Support/JobAppsWorkflowSystem")
        self.assertTrue(str(resolved).endswith(str(expected_suffix)))

    def test_default_app_data_dir_uses_application_support_in_packaged_mode(self) -> None:
        resolved = default_app_data_dir(app_env="packaged")
        expected_suffix = Path("Library/Application Support/JobAppsWorkflowSystem")
        self.assertTrue(str(resolved).endswith(str(expected_suffix)))

    def test_resolve_runtime_path_keeps_relative_path_under_app_data_root(self) -> None:
        app_data_dir = Path("/tmp/job-apps-runtime")
        resolved = resolve_runtime_path("browser-profiles/linkedin", app_data_dir=app_data_dir)
        self.assertEqual(resolved, (app_data_dir / "browser-profiles" / "linkedin").resolve())

    def test_resolve_runtime_path_keeps_absolute_paths(self) -> None:
        absolute = Path("/tmp/custom-linkedin-profile")
        resolved = resolve_runtime_path(str(absolute), app_data_dir=Path("/tmp/ignored"))
        self.assertEqual(resolved, absolute.resolve())

    def test_sqlite_url_for_path(self) -> None:
        path = Path("/tmp/job-apps-runtime/app.db")
        self.assertEqual(sqlite_url_for_path(path), f"sqlite:///{path.resolve()}")

    def test_resolve_database_url_defaults_to_app_db(self) -> None:
        app_data_dir = Path("/tmp/job-apps-runtime")
        resolved = resolve_database_url(None, app_data_dir=app_data_dir)
        self.assertEqual(resolved, sqlite_url_for_path((app_data_dir / "app.db").resolve()))


if __name__ == "__main__":
    unittest.main()
