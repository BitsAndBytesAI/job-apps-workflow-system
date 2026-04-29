from __future__ import annotations

import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.agents.apply.ats_detector import ASHBY, GREENHOUSE, ICIMS, UNKNOWN, detect_ats_type
from job_apps_system.agents.apply.ashby_adapter import (
    AshbyApplyAdapter,
    _extract_ashby_job_id,
    _infer_team_size_option,
    _technology_option_matches,
)
from job_apps_system.agents.apply.ai_browser_loop import AiBrowserApplyLoop
from job_apps_system.agents.apply.greenhouse_adapter import GreenhouseApplyAdapter
from job_apps_system.agents.apply.icims_adapter import IcimsApplyAdapter
from job_apps_system.agents.job_apply import JobApplyAgent
from job_apps_system.config.models import ApplicantProfileConfig, SetupConfig
from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.resumes import ResumeArtifact
from job_apps_system.db.models.unanswered_questions import UnansweredApplicationQuestion
from job_apps_system.schemas.apply import ApplyField, ApplyJobResult
from job_apps_system.services.application_answer_service import (
    ApplicationAnswerService,
    _clean_answer,
    _normalize_question_text,
    infer_structured_choice_candidates,
    infer_structured_yes_no_answer,
)


class ApplyAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=self.engine)
        session_factory = sessionmaker(bind=self.engine, future=True)
        self.session = session_factory()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_detects_ashby_from_company_url_with_ashby_job_id(self) -> None:
        ats_type = detect_ats_type(
            "https://butterflymx.com/careers/?ashby_jid=fa4e4dcb-a5e7-432a-a600-858848a21165"
        )

        self.assertEqual(ats_type, ASHBY)

    def test_detects_ashby_from_direct_ashby_url(self) -> None:
        ats_type = detect_ats_type("https://jobs.ashbyhq.com/butterflymx/fa4e4dcb-a5e7-432a-a600-858848a21165")

        self.assertEqual(ats_type, ASHBY)

    def test_extracts_ashby_job_id_from_query_url(self) -> None:
        self.assertEqual(
            _extract_ashby_job_id(
                "https://jobs.ashbyhq.com/kodex?ashby_jid=cdeadd74-cf90-46e3-a021-1b12cad62018&utm_source=EaJLKlp1lD"
            ),
            "cdeadd74-cf90-46e3-a021-1b12cad62018",
        )

    def test_extracts_ashby_job_id_from_detail_url(self) -> None:
        self.assertEqual(
            _extract_ashby_job_id("https://jobs.ashbyhq.com/butterflymx/fa4e4dcb-a5e7-432a-a600-858848a21165"),
            "fa4e4dcb-a5e7-432a-a600-858848a21165",
        )

    def test_infers_engineering_team_size_from_resume_text(self) -> None:
        applicant = ApplicantProfileConfig(years_of_experience="18")
        resume_text = "Led a team of 8+ software engineers and managed a team of 3-8 onsite engineers."

        self.assertEqual(_infer_team_size_option(applicant, resume_text), "6-8 engineers")

    def test_matches_technology_checkboxes_from_resume_text(self) -> None:
        resume_text = "Expert in React, Node.js, AWS, and PostgreSQL platform work."

        self.assertEqual(
            _technology_option_matches(resume_text),
            {"React", "Node.js", "AWS", "PostgreSQL"},
        )

    def test_clean_answer_strips_refusal_style_text(self) -> None:
        self.assertEqual(
            _clean_answer("This box is asking a question that I cannot answer with the provided information."),
            "",
        )

    def test_greenhouse_location_candidates_include_city_state_variants(self) -> None:
        adapter = GreenhouseApplyAdapter()
        applicant = ApplicantProfileConfig(
            city="Dallas",
            state="Texas",
            country="United States",
            address_line_1="2728 McKinnon St",
        )

        self.assertEqual(
            adapter._location_value_candidates(applicant),
            [
                "Dallas, Texas, United States",
                "Dallas, Texas",
                "Dallas, TX",
                "Dallas, United States",
                "Dallas",
                "2728 McKinnon St, Dallas, Texas, United States",
            ],
        )

    def test_structured_yes_no_infers_work_auth_and_family_relationship(self) -> None:
        applicant = ApplicantProfileConfig(work_authorized_us=True, requires_sponsorship=False)

        self.assertTrue(infer_structured_yes_no_answer("Do you have a legal right to work in the US?", applicant))
        self.assertFalse(
            infer_structured_yes_no_answer(
                "Do you have any family members or persons with whom you have/had a close personal relationship who are employed by Clover Health?",
                applicant,
            )
        )

    def test_structured_choice_candidates_return_yes_no_for_compliance_selects(self) -> None:
        applicant = ApplicantProfileConfig(work_authorized_us=True, requires_sponsorship=False)

        self.assertEqual(
            infer_structured_choice_candidates("Do you have a legal right to work in the US?", applicant),
            ["Yes"],
        )
        self.assertEqual(
            infer_structured_choice_candidates(
                "Will you now or in the future require immigration sponsorship in the United States?",
                applicant,
            ),
            ["No"],
        )

    def test_resume_retry_detection_matches_ashby_required_resume_error(self) -> None:
        adapter = AshbyApplyAdapter()

        self.assertTrue(
            adapter._should_retry_resume_upload(
                "Application submit did not complete; required-field validation is still visible. Visible text: Missing entry for required field Resume"
            )
        )
        self.assertFalse(adapter._should_retry_resume_upload("Blocked by CAPTCHA."))

    def test_normalize_question_text_removes_placeholders_and_duplicates(self) -> None:
        self.assertEqual(
            _normalize_question_text("Additional Information? | Type here... | Additional Information?"),
            "Additional Information?",
        )

    def test_record_unanswered_question_upserts_by_job_and_question(self) -> None:
        service = object.__new__(ApplicationAnswerService)
        service._session = self.session
        config = SetupConfig()
        config.app.project_id = "test-project"
        service._config = config

        self._add_job("gap-job", score=90, apply_url="https://example.com/apply", resume_url="https://drive.example/resume")
        self.session.flush()
        job = self.session.get(Job, "test-project:gap-job")

        service.record_unanswered_question(
            job=job,
            question="Additional Information? | Type here... | Additional Information?",
            ats_type="ashby",
            field_type="textarea",
            required=False,
            reason="llm_empty",
        )
        service.record_unanswered_question(
            job=job,
            question="Additional Information?",
            ats_type="ashby",
            field_type="textarea",
            required=False,
            reason="blank_after_inference",
        )

        rows = self.session.query(UnansweredApplicationQuestion).all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.project_id, "test-project")
        self.assertEqual(row.job_id, "gap-job")
        self.assertEqual(row.question_text, "Additional Information?")
        self.assertEqual(row.occurrence_count, 2)
        self.assertEqual(row.reason, "blank_after_inference")

    def test_detects_greenhouse_from_company_url_with_greenhouse_job_id(self) -> None:
        ats_type = detect_ats_type(
            "https://www.prizepicks.com/position?gh_jid=7701127003&gh_src=351367c33us"
        )

        self.assertEqual(ats_type, GREENHOUSE)

    def test_detects_greenhouse_from_direct_greenhouse_embed_url(self) -> None:
        ats_type = detect_ats_type("https://job-boards.greenhouse.io/embed/job_app?for=prizepicks&token=7701127003")

        self.assertEqual(ats_type, GREENHOUSE)

    def test_detects_icims_from_direct_icims_url(self) -> None:
        ats_type = detect_ats_type(
            "https://careers-cotiviti.icims.com/jobs/18836/manager-engineering-payment-integrity/login"
        )

        self.assertEqual(ats_type, ICIMS)

    def test_unknown_ats_for_non_matching_url(self) -> None:
        self.assertEqual(detect_ats_type("https://example.com/jobs/123"), UNKNOWN)

    def test_adapter_selection_supports_greenhouse(self) -> None:
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(ASHBY), AshbyApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(GREENHOUSE), GreenhouseApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(ICIMS), IcimsApplyAdapter)
        self.assertIsNone(JobApplyAgent._adapter_for_ats(UNKNOWN))

    def test_ai_browser_loop_public_targets_do_not_expose_executable_selectors(self) -> None:
        from job_apps_system.agents.apply.ai_browser_loop import _public_target

        public = _public_target(
            {
                "id": "frame_0:el_001",
                "kind": "field",
                "selector": "[data-apply-agent-id='el_001']",
                "label": "First name",
            }
        )

        self.assertNotIn("selector", public)
        self.assertEqual(public["id"], "frame_0:el_001")

    def test_ai_browser_loop_blocks_generic_click_on_submit_button(self) -> None:
        loop = AiBrowserApplyLoop()

        self.assertTrue(loop._is_submit_target({"text": "Submit Application"}))

    def test_ai_browser_loop_drops_success_logs_by_default(self) -> None:
        loop = AiBrowserApplyLoop(retain_success_logs=False)
        loop._action_log.append({"action": "fill", "status": "success"})
        job = Job(id="job-1", company_name="Example", job_title="Manager")

        result = loop._result(
            job=job,
            detected_ats="unknown",
            status="submitted",
            success=True,
            screenshot_path=Path(__file__),
            confirmation_text="Application submitted.",
            steps=[],
        )

        self.assertEqual(result.action_log, [])

    def test_ai_browser_loop_keeps_non_success_logs(self) -> None:
        loop = AiBrowserApplyLoop(retain_success_logs=False)
        loop._action_log.append({"action": "fill", "status": "failed"})
        job = Job(id="job-1", company_name="Example", job_title="Manager")

        result = loop._result(
            job=job,
            detected_ats="unknown",
            status="needs_review",
            success=False,
            screenshot_path=Path(__file__),
            confirmation_text="Needs review.",
            steps=[],
        )

        self.assertEqual(len(result.action_log), 1)

    def test_ai_browser_loop_allows_apply_labeled_external_navigation(self) -> None:
        loop = AiBrowserApplyLoop()
        page = type("Page", (), {"url": "https://company.example/jobs/1"})()

        self.assertFalse(
            loop._is_obviously_unrelated_navigation(
                page,
                {"href": "https://custom-ats.example/apply/1", "text": "Apply now"},
            )
        )

    def test_ai_browser_loop_blocks_social_navigation(self) -> None:
        loop = AiBrowserApplyLoop()
        page = type("Page", (), {"url": "https://company.example/jobs/1"})()

        self.assertTrue(
            loop._is_obviously_unrelated_navigation(
                page,
                {"href": "https://twitter.com/company", "text": "Twitter"},
            )
        )

    def test_eligible_jobs_require_application_resume_score_and_unapplied_state(self) -> None:
        config = SetupConfig()
        config.app.project_id = "test-project"
        config.app.score_threshold = 82
        agent = object.__new__(JobApplyAgent)
        agent._session = self.session
        agent._project_id = "test-project"
        agent._config = config

        self._add_job("ready", score=90, apply_url="https://jobs.ashbyhq.com/test/ready", resume_url="https://drive.google.com/file/d/resume")
        self._add_resume("ready")
        self._add_job("low-score", score=81, apply_url="https://jobs.ashbyhq.com/test/low", resume_url="https://drive.google.com/file/d/resume")
        self._add_resume("low-score")
        self._add_job("missing-apply", score=90, apply_url=None, resume_url="https://drive.google.com/file/d/resume")
        self._add_resume("missing-apply")
        self._add_job("missing-resume", score=90, apply_url="https://jobs.ashbyhq.com/test/missing", resume_url=None)
        self._add_job("applied", score=90, apply_url="https://jobs.ashbyhq.com/test/applied", resume_url="https://drive.google.com/file/d/resume", applied=True)
        self._add_resume("applied")
        self.session.flush()

        jobs = agent._eligible_jobs(limit=10, job_ids=[], mode="ai")

        self.assertEqual([job.id for job in jobs], ["ready"])

    def test_manual_mode_eligible_jobs_ignore_threshold_and_resume(self) -> None:
        config = SetupConfig()
        config.app.project_id = "test-project"
        config.app.score_threshold = 82
        agent = object.__new__(JobApplyAgent)
        agent._session = self.session
        agent._project_id = "test-project"
        agent._config = config

        self._add_job("manual-job", score=10, apply_url="https://example.com/manual", resume_url=None)
        self.session.flush()

        jobs = agent._eligible_jobs(limit=10, job_ids=["manual-job"], mode="manual")

        self.assertEqual([job.id for job in jobs], ["manual-job"])

    def test_failure_status_maps_captcha_errors(self) -> None:
        result = ApplyJobResult(job_id="job-1", status="failed", error="Blocked by hCaptcha challenge.")

        self.assertEqual(JobApplyAgent._failure_status(result), "captcha")

    def test_record_manual_close_preserves_captcha_state(self) -> None:
        config = SetupConfig()
        config.app.project_id = "test-project"
        agent = object.__new__(JobApplyAgent)
        agent._session = self.session
        agent._project_id = "test-project"
        agent._config = config

        self._add_job("captcha-job", score=90, apply_url="https://example.com/apply", resume_url="https://drive.example/resume")
        self.session.flush()
        job = self.session.get(Job, "test-project:captcha-job")
        job.application_status = "captcha"
        job.application_error = "Blocked by CAPTCHA"

        agent._record_manual_close(
            job,
            ApplyJobResult(job_id="captcha-job", status="manual_closed", screenshot_path="/tmp/test.png"),
        )

        self.assertEqual(job.application_status, "captcha")
        self.assertEqual(job.application_error, "Blocked by CAPTCHA")
        self.assertEqual(job.application_screenshot_path, "/tmp/test.png")

    def test_record_manual_close_sets_captcha_for_manual_recaptcha_fallback(self) -> None:
        config = SetupConfig()
        config.app.project_id = "test-project"
        agent = object.__new__(JobApplyAgent)
        agent._session = self.session
        agent._project_id = "test-project"
        agent._config = config

        self._add_job("manual-captcha-job", score=90, apply_url="https://example.com/apply", resume_url="https://drive.example/resume")
        self.session.flush()
        job = self.session.get(Job, "test-project:manual-captcha-job")

        agent._record_manual_close(
            job,
            ApplyJobResult(
                job_id="manual-captcha-job",
                status="manual_closed",
                confirmation_text="Manual reCAPTCHA completion required in this browser window.",
                screenshot_path="/tmp/manual-captcha.png",
            ),
        )

        self.assertEqual(job.application_status, "captcha")
        self.assertIn("reCAPTCHA", job.application_error)
        self.assertEqual(job.application_screenshot_path, "/tmp/manual-captcha.png")

    def test_ai_recovery_runs_for_failed_adapter_results(self) -> None:
        result = ApplyJobResult(job_id="job-1", status="failed", error="Required field was not filled.")

        self.assertTrue(JobApplyAgent._should_recover_with_ai_browser(result))

    def test_ai_recovery_does_not_run_for_manual_closed_results(self) -> None:
        result = ApplyJobResult(job_id="job-1", status="manual_closed", confirmation_text="Manual window closed.")

        self.assertFalse(JobApplyAgent._should_recover_with_ai_browser(result))

    def test_known_custom_answers_use_applicant_profile(self) -> None:
        applicant = ApplicantProfileConfig(
            years_of_experience="12 years",
            programming_languages_years="Python: 8 years; TypeScript: 6 years",
            favorite_ai_tool="Claude",
            favorite_ai_tool_usage="I use it to review architecture decisions and draft test cases.",
            company_value_example="I took ownership of a delayed migration and rebuilt the execution plan with the team.",
            why_interested_guidance="I am interested because the product solves practical access problems at scale.",
            additional_info_guidance="I can provide references and work samples on request.",
        )
        adapter = AshbyApplyAdapter()

        self.assertEqual(
            adapter._known_custom_answer(
                ApplyField(
                    element_id="programming",
                    tag="textarea",
                    type="textarea",
                    label="List your most proficient programming languages and years of professional experience for each.",
                    selector="[data-apply-agent-id='programming']",
                ),
                applicant,
            ),
            applicant.programming_languages_years,
        )
        self.assertIn(
            "Claude",
            adapter._known_custom_answer(
                ApplyField(
                    element_id="ai_tool",
                    tag="input",
                    type="text",
                    label="What is your favorite AI tool? And how do you use it in your daily work?",
                    selector="[data-apply-agent-id='ai_tool']",
                ),
                applicant,
            ),
        )
        self.assertEqual(
            adapter._known_custom_answer(
                ApplyField(
                    element_id="value",
                    tag="textarea",
                    type="textarea",
                    label="Please pick one of our company values and provide an example of how you have exemplified this value.",
                    selector="[data-apply-agent-id='value']",
                ),
                applicant,
            ),
            applicant.company_value_example,
        )
        self.assertEqual(
            adapter._known_custom_answer(
                ApplyField(
                    element_id="leadership_years",
                    tag="input",
                    type="number",
                    label="How many years of current experience in direct management/leadership do you have?",
                    selector="[data-apply-agent-id='leadership_years']",
                ),
                applicant,
            ),
            "12",
        )

    def test_numeric_custom_answers_are_sanitized_before_fill(self) -> None:
        adapter = AshbyApplyAdapter()
        locator = _FakeLocator()

        adapter._fill_custom_answer(
            locator,
            ApplyField(
                element_id="years",
                tag="input",
                type="number",
                label="Years of experience",
                selector="[data-apply-agent-id='years']",
            ),
            "12 years leading engineering teams",
        )

        self.assertEqual(locator.filled_value, "12")

    def test_numeric_custom_answer_without_number_fails_clearly(self) -> None:
        adapter = AshbyApplyAdapter()
        locator = _FakeLocator()

        with self.assertRaisesRegex(RuntimeError, "Numeric application field could not be answered safely"):
            adapter._fill_custom_answer(
                locator,
                ApplyField(
                    element_id="years",
                    tag="input",
                    type="number",
                    label="Years of experience",
                    selector="[data-apply-agent-id='years']",
                ),
                "extensive leadership background",
            )

    def test_binary_question_answers_use_expected_defaults(self) -> None:
        applicant = ApplicantProfileConfig(requires_sponsorship=True)
        adapter = AshbyApplyAdapter()

        answers = dict(adapter._binary_question_answers(applicant))

        self.assertTrue(
            answers[
                (
                    "worked directly with product managers",
                    "define and deliver against a roadmap",
                )
            ]
        )
        self.assertTrue(
            answers[
                (
                    "employment-based visa sponsorship",
                    "require employment-based visa sponsorship",
                    "will you now or in the future require sponsorship",
                    "require sponsorship",
                )
            ]
        )
        self.assertFalse(answers[("Are you currently in a period of Optimal Practical Training", "Optimal Practical Training")])
        self.assertFalse(
            answers[
                ("24-month OPT extension", "eligible for a 24-month OPT extension", "currently in OPT")
            ]
        )
        self.assertFalse(answers[("Are you a current APFM employee?", "current APFM employee")])
        self.assertFalse(answers[("Were you referred by a current A Place for Mom employee?", "referred by a current A Place for Mom employee")])

    def test_greenhouse_custom_answer_filter_skips_unlabeled_select_proxy_inputs(self) -> None:
        adapter = GreenhouseApplyAdapter()

        self.assertFalse(
            adapter._is_custom_answer_field(
                ApplyField(
                    element_id="select_proxy",
                    tag="input",
                    type="",
                    label="Select...",
                    selector='[data-apply-agent-id="el_014"]',
                )
            )
        )

    def test_greenhouse_custom_answer_filter_skips_combobox_select_inputs(self) -> None:
        adapter = GreenhouseApplyAdapter()

        self.assertFalse(
            adapter._is_custom_answer_field(
                ApplyField(
                    element_id="question_select",
                    tag="input",
                    type="text",
                    label="Have you been employed by Upstart before?* | Select...",
                    selector='[data-apply-agent-id="el_017"]',
                )
            )
        )

    def test_greenhouse_known_combobox_candidates_cover_upstart_required_selects(self) -> None:
        adapter = GreenhouseApplyAdapter()
        applicant = ApplicantProfileConfig(country="United States", requires_sponsorship=False)

        self.assertEqual(
            adapter._known_combobox_candidates(
                ApplyField(
                    element_id="employment",
                    tag="input",
                    type="text",
                    label="Have you been employed by Upstart before?* | Select...",
                    selector='[data-apply-agent-id="el_017"]',
                ),
                applicant,
            ),
            ["No"],
        )
        self.assertEqual(
            adapter._known_combobox_candidates(
                ApplyField(
                    element_id="heard_about",
                    tag="input",
                    type="text",
                    label="Before applying, how did you hear about Upstart?* | Select...",
                    selector='[data-apply-agent-id="el_021"]',
                ),
                applicant,
            )[0],
            "LinkedIn job post",
        )
        self.assertEqual(
            adapter._known_combobox_candidates(
                ApplyField(
                    element_id="location",
                    tag="input",
                    type="text",
                    label="What is your current location?* | Select...",
                    selector='[data-apply-agent-id="el_025"]',
                ),
                applicant,
            ),
            ["United States"],
        )
        self.assertEqual(
            adapter._known_combobox_candidates(
                ApplyField(
                    element_id="gender",
                    tag="input",
                    type="text",
                    label="Gender* | Select...",
                    selector='[data-apply-agent-id="el_027"]',
                ),
                applicant,
            )[0],
            "Decline To Self Identify",
        )

    def test_greenhouse_location_preference_candidates_prefer_remote_for_remote_roles(self) -> None:
        adapter = GreenhouseApplyAdapter()
        applicant = ApplicantProfileConfig(city="Dallas", state="TX", country="United States")
        job = Job(job_title="Senior Engineering Manager", job_description="United States | Remote")

        self.assertEqual(
            adapter._location_preference_candidates(applicant, job)[:2],
            ["Remote", "Austin, TX"],
        )

    def test_greenhouse_manual_completion_fallback_skips_validation_errors(self) -> None:
        adapter = GreenhouseApplyAdapter()

        self.assertFalse(
            adapter._should_use_manual_completion_fallback(
                RuntimeError("Greenhouse submit did not complete; visible field validation remains: Email is required")
            )
        )

    def test_greenhouse_manual_completion_fallback_allows_captcha_or_unknown_submit_blocks(self) -> None:
        adapter = GreenhouseApplyAdapter()

        self.assertTrue(
            adapter._should_use_manual_completion_fallback(
                RuntimeError("Application submit appears blocked by CAPTCHA. Visible text: submit")
            )
        )
        self.assertTrue(
            adapter._should_use_manual_completion_fallback(
                RuntimeError("Could not verify successful Greenhouse application submission. Visible text: review your application")
            )
        )

    def _add_job(
        self,
        job_id: str,
        *,
        score: int,
        apply_url: str | None,
        resume_url: str | None,
        applied: bool = False,
    ) -> None:
        self.session.add(
            Job(
                record_id=f"test-project:{job_id}",
                project_id="test-project",
                id=job_id,
                company_name=f"{job_id} company",
                job_title="Engineering Manager",
                job_description="Build engineering systems.",
                intake_decision="accepted",
                score=score,
                applied=applied,
                apply_url=apply_url,
                resume_url=resume_url,
            )
        )

    def _add_resume(self, job_id: str) -> None:
        self.session.add(
            ResumeArtifact(
                id=f"test-project:{job_id}",
                project_id="test-project",
                job_id=job_id,
                pdf_drive_file_id=f"drive-{job_id}",
                pdf_drive_url=f"https://drive.google.com/file/d/drive-{job_id}",
                status="generated",
            )
        )

class _FakeLocator:
    def __init__(self) -> None:
        self.filled_value: str | None = None

    def fill(self, value: str, timeout: int = 0) -> None:
        self.filled_value = value


if __name__ == "__main__":
    unittest.main()
