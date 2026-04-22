from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.agents.resume_generation import ResumeGenerationAgent
from job_apps_system.config.models import SetupConfig
from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.settings import AppSetting
from job_apps_system.services.setup_config import SETUP_CONFIG_KEY


class ResumeGenerationAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = session_factory()

        config = SetupConfig()
        config.app.project_id = "test-project"
        config.app.project_name = "Test Project"
        config.app.job_role = "Engineering Manager"
        config.app.score_threshold = 82
        self.session.add(
            AppSetting(
                key=SETUP_CONFIG_KEY,
                value_json=json.dumps(
                    config.model_dump(
                        mode="json",
                        exclude={
                            "secrets": True,
                            "field_validations": True,
                            "google": {"managed_resources"},
                        },
                    )
                ),
            )
        )

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_pending_resume_jobs_are_limited_to_scored_applications_at_or_above_threshold(self) -> None:
        self._add_job("high", score=90)
        self._add_job("equal", score=82)
        self._add_job("low", score=81)
        self._add_job("unscored", score=None)
        self._add_job("duplicate", score=95, intake_decision="rejected")
        self._add_job("existing-resume", score=95, resume_url="https://example.com/resume.pdf")
        self._add_job("missing-description", score=95, job_description="")
        self.session.flush()

        pending = ResumeGenerationAgent(self.session)._get_pending_jobs(limit=None, job_ids=None)

        self.assertEqual([job.job_id for job in pending], ["equal", "high"])

    def test_explicit_job_ids_do_not_bypass_application_threshold(self) -> None:
        self._add_job("high", score=90)
        self._add_job("low", score=81)
        self.session.flush()

        pending = ResumeGenerationAgent(self.session)._get_pending_jobs(
            limit=None,
            job_ids=["high", "low"],
        )

        self.assertEqual([job.job_id for job in pending], ["high"])

    def test_resume_formatter_removes_bulleted_horizontal_rules_under_sections(self) -> None:
        markdown_text = """# Candidate Name
candidate@example.com | linkedin.com/in/candidate
Engineering Manager

## Professional Summary
Leads engineering teams.

## Professional Certifications
- ---
- AWS Certified Solutions Architect
"""

        html = ResumeGenerationAgent(self.session)._format_resume_html(markdown_text)

        self.assertNotIn("<hr", html.lower())
        self.assertNotIn("border-bottom", html.lower())
        self.assertNotRegex(html.lower(), r"<li[^>]*>\s*<hr")
        self.assertIn("AWS Certified Solutions Architect", html)

    def _add_job(
        self,
        job_id: str,
        *,
        score: int | None,
        intake_decision: str | None = "accepted",
        resume_url: str | None = None,
        job_description: str = "Build systems and lead teams.",
    ) -> None:
        self.session.add(
            Job(
                record_id=f"test-project:{job_id}",
                project_id="test-project",
                id=job_id,
                company_name=f"{job_id} company",
                job_title="Engineering Manager",
                job_description=job_description,
                intake_decision=intake_decision,
                score=score,
                applied=False,
                resume_url=resume_url,
            )
        )


if __name__ == "__main__":
    unittest.main()
