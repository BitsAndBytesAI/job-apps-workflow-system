from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.db.models.workflow_runs import WorkflowRun
from job_apps_system.services import manual_runs


class ManualRunPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = self.session_factory()
        self._clear_run_state()

    def tearDown(self) -> None:
        self._clear_run_state()
        self.session.close()
        self.engine.dispose()

    def test_update_active_run_does_not_fail_when_snapshot_queue_fails(self) -> None:
        with manual_runs._ACTIVE_RUNS_LOCK:
            manual_runs._ACTIVE_RUNS["run-1"] = {
                "id": "run-1",
                "agent_name": "job_intake",
                "status": "running",
                "message": "Starting",
                "steps": [],
                "run_payload": {},
            }

        with self.assertLogs(manual_runs.logger, level="ERROR"):
            with patch.object(manual_runs, "_queue_run_snapshot", side_effect=RuntimeError("queue failed")):
                payload = manual_runs.update_active_run(
                    "run-1",
                    message="Still running",
                    step_name="Analyze",
                    step_status="running",
                )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["message"], "Still running")
        self.assertEqual(payload["steps"][0]["name"], "Analyze")

    def test_stale_running_snapshot_does_not_overwrite_final_run_status(self) -> None:
        row = WorkflowRun(
            id="run-1",
            project_id="test-project",
            trigger_type="manual",
            status="succeeded",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            summary_json='{"agent_name": "job_intake", "message": "Done."}',
        )
        self.session.add(row)
        self.session.commit()

        with patch.object(manual_runs, "SessionLocal", self.session_factory):
            persisted = manual_runs._persist_run_snapshot_best_effort(
                "run-1",
                {
                    "id": "run-1",
                    "agent_name": "job_intake",
                    "status": "running",
                    "message": "Old progress",
                    "steps": [],
                    "run_payload": {},
                },
            )

        self.assertTrue(persisted)
        self.session.expire_all()
        self.assertEqual(self.session.get(WorkflowRun, "run-1").status, "succeeded")

    def test_create_manual_run_persists_trigger_source(self) -> None:
        run = manual_runs.create_manual_run(
            self.session,
            agent_name="job_intake",
            project_id="test-project",
            trigger_type="manual",
            trigger_source="dashboard_find_jobs_card",
            run_payload={"search_urls": []},
        )
        self.session.commit()

        self.assertEqual(run["trigger_source"], "dashboard_find_jobs_card")
        persisted = manual_runs.get_run(self.session, run["id"])
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted["trigger_source"], "dashboard_find_jobs_card")

    def _clear_run_state(self) -> None:
        with manual_runs._ACTIVE_RUNS_LOCK:
            manual_runs._ACTIVE_RUNS.clear()
        with manual_runs._SNAPSHOT_WRITER_CONDITION:
            manual_runs._PENDING_SNAPSHOTS.clear()


if __name__ == "__main__":
    unittest.main()
