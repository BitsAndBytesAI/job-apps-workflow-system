from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.schemas.schedule import AgentScheduleConfig, SchedulerConfigPayload
from job_apps_system.services import scheduler as scheduler_service


class SchedulerTickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = session_factory()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_tick_runs_due_agent_once_per_slot(self) -> None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        now = datetime(2026, 4, 23, 14, 30, tzinfo=local_tz)
        current_day = scheduler_service.SCHEDULE_DAY_OPTIONS[now.weekday()]
        scheduler_service.save_scheduler_config(
            self.session,
            SchedulerConfigPayload(
                schedules=[
                    AgentScheduleConfig(
                        agent_name="job_intake",
                        enabled=True,
                        days_of_week=[current_day],
                        run_at_local_time=now.strftime("%H:%M"),
                    ),
                    AgentScheduleConfig(agent_name="job_scoring", enabled=False, days_of_week=[current_day], run_at_local_time="09:00"),
                    AgentScheduleConfig(agent_name="resume_generation", enabled=False, days_of_week=[current_day], run_at_local_time="09:00"),
                ]
            ),
        )

        fake_run = {
            "id": "run-1",
            "status": "succeeded",
            "message": "Scheduled run finished.",
            "started_at": now.isoformat(),
            "finished_at": now.isoformat(),
        }

        with patch.object(scheduler_service, "run_scheduled_agent", return_value=fake_run) as mocked_runner:
            first = scheduler_service.run_scheduler_tick(self.session, now=now)
            second = scheduler_service.run_scheduler_tick(self.session, now=now)

        self.assertEqual(first.triggered_agents, ["job_intake"])
        self.assertEqual(second.triggered_agents, [])
        self.assertEqual(mocked_runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
