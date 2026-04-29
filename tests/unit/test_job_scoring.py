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

    def test_parse_scoring_payload_reads_json(self) -> None:
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

        parsed = JobScoringAgent._parse_scoring_payload(object.__new__(JobScoringAgent), payload)

        self.assertEqual(parsed["overall"], 872.44)

    def test_compute_score_redistributes_na_and_caps_missing_evidence(self) -> None:
        payload = {
            "verdict": "plausible",
            "dimensions": [
                {"name": "Required hard-skill coverage", "score": 9.8, "weight": 18, "evidence_resume": "", "evidence_jd": "must have kubernetes"},
                {"name": "Skill recency", "score": None, "weight": 8, "evidence_resume": "", "evidence_jd": "", "na_reason": "resume does not say when last used"},
                {"name": "Skill depth", "score": 8.0, "weight": 10, "evidence_resume": "owned platform for 4 years", "evidence_jd": "needs ownership"},
                {"name": "Years of experience vs. JD ask", "score": 8.0, "weight": 8, "evidence_resume": "15 years experience", "evidence_jd": "12+ years"},
                {"name": "Seniority/scope alignment", "score": 8.0, "weight": 10, "evidence_resume": "managed directors", "evidence_jd": "senior leadership role"},
                {"name": "Domain/industry match", "score": 8.0, "weight": 8, "evidence_resume": "healthcare platform", "evidence_jd": "healthcare company"},
                {"name": "Stack specificity", "score": 8.0, "weight": 8, "evidence_resume": "AWS, Kubernetes", "evidence_jd": "AWS, Kubernetes"},
                {"name": "Role-shape match", "score": 8.0, "weight": 6, "evidence_resume": "engineering leadership", "evidence_jd": "manager role"},
                {"name": "Trajectory fit", "score": 8.0, "weight": 6, "evidence_resume": "director scope", "evidence_jd": "director role"},
                {"name": "Company-stage match", "score": 8.0, "weight": 4, "evidence_resume": "public company", "evidence_jd": "public company"},
                {"name": "Mission/product affinity signal", "score": 8.0, "weight": 6, "evidence_resume": "AI product work", "evidence_jd": "AI platform"},
                {"name": "Logistics fit", "score": 8.0, "weight": 4, "evidence_resume": "US based", "evidence_jd": "US remote"},
                {"name": "Differentiator presence", "score": 8.0, "weight": 4, "evidence_resume": "rare credential", "evidence_jd": "preferred credential"},
            ],
            "modifiers_applied": [],
            "top_strengths": [],
            "top_gaps": [],
            "single_sentence_summary": "Good fit.",
        }

        score = JobScoringAgent._compute_score(object.__new__(JobScoringAgent), payload)

        self.assertEqual(score, 742)

    def test_compute_score_caps_disqualified_roles_below_200(self) -> None:
        payload = {
            "verdict": "disqualified",
            "dimensions": [
                {"name": "Required hard-skill coverage", "score": 10.0, "weight": 18, "evidence_resume": "quote 1", "evidence_jd": "quote 2"},
                {"name": "Skill recency", "score": 10.0, "weight": 8, "evidence_resume": "quote 3", "evidence_jd": "quote 4"},
                {"name": "Skill depth", "score": 10.0, "weight": 10, "evidence_resume": "quote 5", "evidence_jd": "quote 6"},
                {"name": "Years of experience vs. JD ask", "score": 10.0, "weight": 8, "evidence_resume": "quote 7", "evidence_jd": "quote 8"},
                {"name": "Seniority/scope alignment", "score": 10.0, "weight": 10, "evidence_resume": "quote 9", "evidence_jd": "quote 10"},
                {"name": "Domain/industry match", "score": 10.0, "weight": 8, "evidence_resume": "quote 11", "evidence_jd": "quote 12"},
                {"name": "Stack specificity", "score": 10.0, "weight": 8, "evidence_resume": "quote 13", "evidence_jd": "quote 14"},
                {"name": "Role-shape match", "score": 10.0, "weight": 6, "evidence_resume": "quote 15", "evidence_jd": "quote 16"},
                {"name": "Trajectory fit", "score": 10.0, "weight": 6, "evidence_resume": "quote 17", "evidence_jd": "quote 18"},
                {"name": "Company-stage match", "score": 10.0, "weight": 4, "evidence_resume": "quote 19", "evidence_jd": "quote 20"},
                {"name": "Mission/product affinity signal", "score": 10.0, "weight": 6, "evidence_resume": "quote 21", "evidence_jd": "quote 22"},
                {"name": "Logistics fit", "score": 10.0, "weight": 4, "evidence_resume": "quote 23", "evidence_jd": "quote 24"},
                {"name": "Differentiator presence", "score": 10.0, "weight": 4, "evidence_resume": "quote 25", "evidence_jd": "quote 26"},
            ],
            "modifiers_applied": [{"name": "Hard disqualifier present", "delta": -150, "reason": "Requires clearance candidate lacks"}],
            "top_strengths": [],
            "top_gaps": [],
            "single_sentence_summary": "Disqualified.",
        }

        score = JobScoringAgent._compute_score(object.__new__(JobScoringAgent), payload)

        self.assertLess(score, 200)
