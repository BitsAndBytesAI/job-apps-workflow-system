from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.anymailfinder.client import DecisionMakerResult
from job_apps_system.services.interview_contacts import (
    load_contacts_by_job,
    refresh_job_contacts,
    update_contact_selected,
)


class InterviewContactsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = session_factory()
        self.job = Job(
            record_id="project-1:job-123",
            project_id="project-1",
            id="job-123",
            company_name="Acme",
            job_title="Senior Software Engineer",
            job_description="Build internal tooling and platform APIs.",
            company_url="https://www.linkedin.com/company/acme/",
            apply_url="https://jobs.lever.co/acme/job-123",
            created_time=datetime.now(timezone.utc),
            applied=True,
        )
        self.session.add(self.job)
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_refresh_job_contacts_fetches_role_ceo_and_hr_and_persists_rows(self) -> None:
        responses = [
            DecisionMakerResult(
                decision_maker_category="engineering",
                email="eng@example.com",
                email_status="valid",
                person_full_name="Eng Lead",
                person_job_title="VP Engineering",
                person_linkedin_url="https://linkedin.com/in/englead",
                valid_email="eng@example.com",
            ),
            DecisionMakerResult(
                decision_maker_category="ceo",
                email="ceo@example.com",
                email_status="valid",
                person_full_name="Chief Exec",
                person_job_title="CEO",
                person_linkedin_url=None,
                valid_email="ceo@example.com",
            ),
            DecisionMakerResult(
                decision_maker_category="hr",
                email="hr@example.com",
                email_status="risky",
                person_full_name="People Lead",
                person_job_title="Head of HR",
                person_linkedin_url=None,
                valid_email=None,
            ),
        ]

        with (
            patch("job_apps_system.services.interview_contacts.get_secret", return_value="api-key"),
            patch(
                "job_apps_system.services.interview_contacts.find_decision_maker_email",
                side_effect=responses,
            ) as mocked_lookup,
        ):
            contacts = refresh_job_contacts(self.session, self.job)

        self.assertEqual([contact["decision_maker_category"] for contact in contacts], ["engineering", "ceo", "hr"])
        self.assertEqual(mocked_lookup.call_args_list[0].kwargs["company_name"], "Acme")
        self.assertIsNone(mocked_lookup.call_args_list[0].kwargs["domain"])
        grouped = load_contacts_by_job(self.session, "project-1", ["job-123"])
        self.assertEqual(len(grouped["job-123"]), 3)

    def test_update_contact_selected_persists_checkbox_state(self) -> None:
        with (
            patch("job_apps_system.services.interview_contacts.get_secret", return_value="api-key"),
            patch(
                "job_apps_system.services.interview_contacts.find_decision_maker_email",
                return_value=DecisionMakerResult(
                    decision_maker_category="engineering",
                    email="eng@example.com",
                    email_status="valid",
                    person_full_name="Eng Lead",
                    person_job_title="VP Engineering",
                    person_linkedin_url=None,
                    valid_email="eng@example.com",
                ),
            ),
        ):
            contacts = refresh_job_contacts(self.session, self.job)

        updated = update_contact_selected(
            self.session,
            project_id="project-1",
            job_id="job-123",
            contact_id=contacts[0]["id"],
            selected=True,
        )

        self.assertTrue(updated["selected"])
        grouped = load_contacts_by_job(self.session, "project-1", ["job-123"])
        self.assertTrue(grouped["job-123"][0]["selected"])


if __name__ == "__main__":
    unittest.main()
