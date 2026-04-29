from __future__ import annotations

import unittest

from job_apps_system.agents.job_scoring import JobScoringAgent
from job_apps_system.config.models import SetupConfig
from job_apps_system.db.models.jobs import Job


class JobScoringPromptTests(unittest.TestCase):
    def test_scoring_prompt_uses_stored_base_resume_text(self) -> None:
        agent = object.__new__(JobScoringAgent)
        config = SetupConfig()
        config.app.job_role = "Engineering Manager"
        config.project_resume.extracted_text = "Led teams of engineers, built cloud platforms, and delivered AWS systems."
        agent._config = config

        job = Job(
            id="job-1",
            project_id="test-project",
            company_name="Example Co",
            job_title="Director of Engineering",
            job_description="Looking for an engineering leader with AWS platform experience.",
        )

        prompt = agent._build_user_prompt(job)

        self.assertIn("Rubric:", prompt)
        self.assertIn("Base resume:", prompt)
        self.assertIn(config.project_resume.extracted_text, prompt)
        self.assertIn('"target_role": "Engineering Manager"', prompt)
        self.assertNotIn("Below is a list of my skills summary", prompt)
        self.assertNotIn("Below is a list of my resume keywords", prompt)
        self.assertNotIn("salesforce", prompt.lower())

    def test_parse_score_reads_rubric_overall_value(self) -> None:
        payload = """
        {
          "overall": 872.44,
          "verdict": "strong",
          "dimensions": [],
          "modifiers_applied": [],
          "top_strengths": [],
          "top_gaps": [],
          "single_sentence_summary": "Strong fit."
        }
        """

        score = JobScoringAgent._parse_score(object.__new__(JobScoringAgent), payload)

        self.assertEqual(score, 872)
