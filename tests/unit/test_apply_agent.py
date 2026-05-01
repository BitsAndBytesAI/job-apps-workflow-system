from __future__ import annotations

import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.agents.apply.ats_detector import (
    ASHBY,
    DICE,
    GREENHOUSE,
    ICIMS,
    LEVER,
    LINKEDIN,
    ORACLE_CLOUD,
    UNKNOWN,
    WORKDAY,
    detect_ats_type,
)
from job_apps_system.agents.apply.ashby_adapter import (
    AshbyApplyAdapter,
    _extract_ashby_job_id,
    _infer_team_size_option,
    _technology_option_matches,
)
from job_apps_system.agents.apply.ai_browser_loop import (
    AiBrowserApplyLoop,
    ManualHandoffRequested,
    _looks_like_active_manual_verification,
    _looks_like_auth_gate_text,
    _looks_like_interactive_manual_verification,
    _looks_like_verification_retry_error,
)
from job_apps_system.agents.apply.dice_adapter import DiceApplyAdapter
from job_apps_system.agents.apply.greenhouse_adapter import GreenhouseApplyAdapter
from job_apps_system.agents.apply.icims_adapter import IcimsApplyAdapter
from job_apps_system.agents.apply.lever_adapter import LeverAiBrowserApplyLoop, LeverApplyAdapter
from job_apps_system.agents.apply.oracle_cloud_adapter import OracleCloudApplyAdapter, is_oracle_cloud_page
from job_apps_system.agents.job_apply import (
    JobApplyAgent,
    _company_name_from_url,
    _should_store_discovered_apply_url,
    _should_store_discovered_company_name,
)
from job_apps_system.config.models import ApplicantProfileConfig, SetupConfig
from job_apps_system.db import models  # noqa: F401
from job_apps_system.db.base import Base
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.resumes import ResumeArtifact
from job_apps_system.db.models.unanswered_questions import UnansweredApplicationQuestion
from job_apps_system.schemas.apply import ApplyAction, ApplyField, ApplyJobResult
from job_apps_system.services.application_answer_service import (
    ApplicationAnswerService,
    _clean_answer,
    _normalize_question_text,
    infer_structured_choice_candidates,
    infer_structured_yes_no_answer,
)
from job_apps_system.services.applicant_names import applicant_name_for_label, applicant_name_parts
from job_apps_system.services.apply_site_sessions import site_key_for_url


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

    def test_discovered_apply_url_ignores_job_board_intermediaries(self) -> None:
        current = "https://click.appcast.io/t/GGS89joOM5qXMSKAvxX221d2Em152c2g9d0pziON-6Y="

        self.assertFalse(_should_store_discovered_apply_url("https://www.dice.com/job-detail/fed924df", current))
        self.assertFalse(_should_store_discovered_apply_url("https://www.linkedin.com/jobs/view/4403790746/", current))

    def test_discovered_apply_url_accepts_employer_destination(self) -> None:
        current = "https://click.appcast.io/t/GGS89joOM5qXMSKAvxX221d2Em152c2g9d0pziON-6Y="

        self.assertTrue(_should_store_discovered_apply_url("https://www.kforce.com/jobs/job-details/123", current))

    def test_discovered_company_name_replaces_generic_dice_company(self) -> None:
        self.assertEqual(_company_name_from_url("https://careers.kforce.com/jobs/123"), "Kforce")
        self.assertTrue(_should_store_discovered_company_name("Kforce", "Jobs via Dice"))

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

    def test_applicant_name_parts_use_preferred_first_name_unless_legal_requested(self) -> None:
        applicant = ApplicantProfileConfig(legal_name="Kirk Rohani", preferred_name="Khurrum")
        names = applicant_name_parts(applicant)

        self.assertEqual(names.first_name, "Khurrum")
        self.assertEqual(names.last_name, "Rohani")
        self.assertEqual(names.full_name, "Khurrum Rohani")
        self.assertEqual(names.legal_first_name, "Kirk")
        self.assertEqual(names.legal_name, "Kirk Rohani")
        self.assertEqual(applicant_name_for_label("First Name", applicant), "Khurrum")
        self.assertEqual(applicant_name_for_label("Given Name", applicant), "Khurrum")
        self.assertEqual(applicant_name_for_label("Full Name", applicant), "Khurrum Rohani")
        self.assertEqual(applicant_name_for_label("Legal First Name", applicant), "Kirk")
        self.assertEqual(applicant_name_for_label("Legal Name", applicant), "Kirk Rohani")

    def test_structured_yes_no_infers_work_auth_and_family_relationship(self) -> None:
        applicant = ApplicantProfileConfig(work_authorized_us=True, requires_sponsorship=False)

        self.assertTrue(infer_structured_yes_no_answer("Are you at least 18 years of age?", applicant))
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
        self.assertEqual(
            infer_structured_choice_candidates(
                "In the future, will you require the company's sponsorship or need the company's assistance to obtain or maintain authorization to work legally in the United States?",
                applicant,
            ),
            ["No"],
        )

    def test_structured_choice_candidates_handle_military_self_identification(self) -> None:
        applicant = ApplicantProfileConfig()

        self.assertIn(
            "I do not wish to answer",
            infer_structured_choice_candidates("Military Status", applicant),
        )
        self.assertEqual(
            infer_structured_choice_candidates("Military Spouse/Domestic Partner", applicant)[0],
            "No",
        )

    def test_ai_loop_allows_overwriting_obviously_wrong_autofill_values(self) -> None:
        loop = AiBrowserApplyLoop()

        self.assertFalse(
            loop._should_preserve_existing_value(
                None,
                {
                    "has_value": True,
                    "current_value": "(972) 800-4348",
                    "label": "Full Name",
                    "type": "text",
                },
                replacement="Kirk Rohani",
            )
        )
        self.assertFalse(
            loop._should_preserve_existing_value(
                None,
                {
                    "has_value": True,
                    "current_value": "Kirk",
                    "label": "Link 1",
                    "type": "text",
                },
                replacement="https://www.linkedin.com/in/example",
            )
        )
        self.assertTrue(
            loop._should_preserve_existing_value(
                None,
                {
                    "has_value": True,
                    "current_value": "Kirk Rohani",
                    "label": "Full Name",
                    "type": "text",
                },
                replacement="Kirk Rohani",
            )
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

    def test_detects_dice_job_board_url(self) -> None:
        self.assertEqual(
            detect_ats_type("https://www.dice.com/job-detail/fed924df-0ad6-4dc5-8170-b2e915c031d4"),
            DICE,
        )

    def test_detects_common_authenticated_apply_hosts(self) -> None:
        self.assertEqual(detect_ats_type("https://www.linkedin.com/jobs/view/4405566508/"), LINKEDIN)
        self.assertEqual(
            detect_ats_type(
                "https://jobs.lever.co/aledade/7d45c12a-a8d3-4f6c-a621-f2c092f533b5/apply?source=LinkedIn"
            ),
            LEVER,
        )
        self.assertEqual(
            detect_ats_type("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/123"),
            ORACLE_CLOUD,
        )
        self.assertEqual(detect_ats_type("https://company.wd1.myworkdaysite.com/recruiting/job/123"), WORKDAY)

    def test_site_key_for_common_authenticated_apply_hosts(self) -> None:
        self.assertEqual(site_key_for_url("https://www.linkedin.com/jobs/view/4405566508/"), "linkedin")
        self.assertEqual(site_key_for_url("https://jpmc.fa.oraclecloud.com/hcmUI/jobs/123"), "oracle-cloud")
        self.assertEqual(site_key_for_url("https://company.wd1.myworkdaysite.com/recruiting/job/123"), "workday")
        self.assertEqual(site_key_for_url("https://www.dice.com/job-detail/abc"), "dice")
        self.assertEqual(site_key_for_url("https://jobs.lever.co/aledade/job-id/apply?source=LinkedIn"), "lever")

    def test_jpmc_oracle_cloud_url_maps_to_jpmorgan_chase(self) -> None:
        self.assertEqual(
            _company_name_from_url("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/123"),
            "JPMorganChase",
        )
        self.assertFalse(_should_store_discovered_company_name("JPMC Candidate Experience page", "JPMorganChase"))

    def test_adapter_selection_supports_greenhouse(self) -> None:
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(ASHBY), AshbyApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(GREENHOUSE), GreenhouseApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(ICIMS), IcimsApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(DICE), DiceApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(ORACLE_CLOUD), OracleCloudApplyAdapter)
        self.assertIsInstance(JobApplyAgent._adapter_for_ats(LEVER), LeverApplyAdapter)
        self.assertIsNone(JobApplyAgent._adapter_for_ats(UNKNOWN))

    def test_dice_adapter_canonicalizes_job_detail_urls(self) -> None:
        adapter = DiceApplyAdapter()

        self.assertEqual(
            adapter._canonical_job_url("https://www.dice.com/job-detail/abc123?utm_source=appcast"),
            "https://www.dice.com/job-detail/abc123",
        )
        self.assertIsNone(adapter._canonical_job_url("https://www.dice.com/companies"))

    def test_dice_adapter_builds_start_apply_url_from_job_detail(self) -> None:
        adapter = DiceApplyAdapter()

        self.assertEqual(
            adapter._dice_start_apply_url("https://www.dice.com/job-detail/fed924df-0ad6-4dc5-8170-b2e915c031d4"),
            "https://www.dice.com/job-applications/fed924df-0ad6-4dc5-8170-b2e915c031d4/start-apply",
        )

    def test_dice_adapter_extracts_start_apply_redirect(self) -> None:
        adapter = DiceApplyAdapter()
        url = (
            "https://www.dice.com/dashboard/login?"
            "redirectUrl=%2Fjob-applications%2Ffed924df-0ad6-4dc5-8170-b2e915c031d4%2Fstart-apply"
        )

        self.assertEqual(
            adapter._dice_application_url_from_candidate(url),
            "https://www.dice.com/job-applications/fed924df-0ad6-4dc5-8170-b2e915c031d4/start-apply",
        )

    def test_dice_adapter_only_honors_explicit_user_manual_choice(self) -> None:
        adapter = DiceApplyAdapter()

        self.assertFalse(
            adapter._should_stop_for_manual_result(
                ApplyJobResult(job_id="job-1", status="manual_closed", steps=["AI loop requested manual completion."])
            )
        )
        self.assertTrue(
            adapter._should_stop_for_manual_result(
                ApplyJobResult(job_id="job-1", status="manual_closed", steps=["User chose to finish the application manually."])
            )
        )

    def test_dice_adapter_allows_one_start_apply_retry_after_profile_detour(self) -> None:
        adapter = DiceApplyAdapter()
        result = ApplyJobResult(
            job_id="job-1",
            status="needs_review",
            confirmation_text="Dice profile or registration detour has no actionable form fields; returning control to the Dice adapter.",
        )

        self.assertFalse(adapter._should_request_profile_completion(result, start_apply_retries=0))
        self.assertTrue(adapter._should_request_profile_completion(result, start_apply_retries=1))

    def test_dice_adapter_clicks_profile_cta_once_after_profile_detour(self) -> None:
        adapter = DiceApplyAdapter()
        result = ApplyJobResult(
            job_id="job-1",
            status="needs_review",
            confirmation_text="Dice profile or registration detour has no actionable form fields; returning control to the Dice adapter.",
        )

        self.assertTrue(adapter._should_click_profile_cta(result, profile_cta_clicks=0))
        self.assertFalse(adapter._should_click_profile_cta(result, profile_cta_clicks=1))

    def test_dice_adapter_does_not_request_profile_completion_for_other_needs_review(self) -> None:
        adapter = DiceApplyAdapter()
        result = ApplyJobResult(
            job_id="job-1",
            status="needs_review",
            confirmation_text="Dice gateway is complete; continue with AI browser apply loop.",
        )

        self.assertFalse(adapter._should_request_profile_completion(result, start_apply_retries=1))

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

    def test_ai_browser_loop_blocks_generic_click_on_final_submit_button(self) -> None:
        loop = AiBrowserApplyLoop()

        self.assertTrue(loop._should_require_submit_application_action({"text": "Submit Application"}))

    def test_ai_browser_loop_allows_apply_now_entry_click_without_form_fields(self) -> None:
        loop = AiBrowserApplyLoop()

        self.assertFalse(loop._should_require_submit_application_action({"text": "Apply Now", "tag": "button"}, {}))

    def test_ai_browser_loop_treats_apply_now_as_final_submit_when_form_fields_exist(self) -> None:
        loop = AiBrowserApplyLoop()
        targets = {
            "frame_0:field_001": {
                "kind": "field",
                "label": "First name",
                "type": "text",
                "required": True,
            }
        }

        self.assertTrue(
            loop._should_require_submit_application_action({"text": "Apply Now", "tag": "button"}, targets)
        )

    def test_ai_browser_loop_allows_apply_labeled_anchor_entry_with_form_fields(self) -> None:
        loop = AiBrowserApplyLoop()
        targets = {
            "frame_0:field_001": {
                "kind": "field",
                "label": "First name",
                "type": "text",
                "required": True,
            }
        }

        self.assertFalse(
            loop._should_require_submit_application_action(
                {"text": "Apply Now", "tag": "a", "href": "https://www.dice.com/dashboard/login"},
                targets,
            )
        )

    def test_ai_browser_loop_selects_dice_job_detail_apply_now_even_with_page_fields(self) -> None:
        loop = AiBrowserApplyLoop()
        page = type("Page", (), {"url": "https://www.dice.com/job-detail/fed924df-0ad6-4dc5-8170-b2e915c031d4"})()
        targets = {
            "frame_0:field_001": {
                "id": "frame_0:field_001",
                "kind": "field",
                "label": "Email",
                "type": "email",
                "required": True,
            },
            "frame_0:btn_012": {
                "id": "frame_0:btn_012",
                "kind": "button",
                "text": "Apply Now",
                "tag": "button",
                "href": "",
                "disabled": False,
            },
        }

        target = loop._select_application_entry_target(page, DICE, targets)

        self.assertIsNotNone(target)
        self.assertEqual(target["id"], "frame_0:btn_012")

    def test_ai_browser_loop_does_not_select_apply_now_on_non_dice_form_page(self) -> None:
        loop = AiBrowserApplyLoop()
        page = type("Page", (), {"url": "https://example.com/application"})()
        targets = {
            "frame_0:field_001": {
                "id": "frame_0:field_001",
                "kind": "field",
                "label": "Email",
                "type": "email",
                "required": True,
            },
            "frame_0:btn_001": {
                "id": "frame_0:btn_001",
                "kind": "button",
                "text": "Apply Now",
                "tag": "button",
                "href": "",
                "disabled": False,
            },
        }

        self.assertIsNone(loop._select_application_entry_target(page, "unknown", targets))

    def test_ai_browser_loop_detects_linkedin_join_modal_as_auth_gate(self) -> None:
        text = "Agree & Join LinkedIn Continue with Google Join with email Already on LinkedIn? Sign in"

        self.assertTrue(_looks_like_auth_gate_text(text))
        self.assertTrue(AiBrowserApplyLoop()._has_hard_stop(["login_required"]))

    def test_ai_browser_loop_does_not_treat_plain_job_alert_sign_in_as_auth_gate(self) -> None:
        text = "Get notified about new Coach jobs in United States. Sign in to create job alert."

        self.assertFalse(_looks_like_auth_gate_text(text))

    def test_ai_browser_loop_ignores_passive_recaptcha_disclaimer(self) -> None:
        self.assertFalse(
            _looks_like_active_manual_verification(
                "This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply."
            )
        )

    def test_ai_browser_loop_detects_active_captcha_challenge(self) -> None:
        self.assertTrue(_looks_like_active_manual_verification("Please verify you are human before continuing."))
        self.assertTrue(
            _looks_like_active_manual_verification(
                "",
                iframe_sources="https://www.google.com/recaptcha/api2/anchor?k=test",
            )
        )
        self.assertTrue(
            _looks_like_active_manual_verification(
                "Help the fish get to the other end by dragging the pipe."
            )
        )
        self.assertTrue(
            _looks_like_interactive_manual_verification(
                "",
                iframe_sources="https://client-api.arkoselabs.com/fc/gc/?token=test",
            )
        )

    def test_ai_browser_loop_detects_verification_retry_error(self) -> None:
        self.assertTrue(
            _looks_like_verification_retry_error(
                "There was an error verifying your application. Please try again."
            )
        )

    def test_ai_browser_loop_defers_manual_verification_while_fields_remain_actionable(self) -> None:
        loop = AiBrowserApplyLoop()
        observation = {
            "blockers": ["manual_verification"],
            "frames": [{"fields": [{"type": "email", "label": "Email"}], "buttons": []}],
        }

        self.assertFalse(loop._manual_verification_requires_handoff(observation))
        loop._submit_attempts = 1
        self.assertTrue(loop._manual_verification_requires_handoff(observation))

    def test_ai_browser_loop_hands_off_interactive_captcha_even_with_fields(self) -> None:
        loop = AiBrowserApplyLoop()
        observation = {
            "blockers": ["manual_verification", "interactive_verification"],
            "frames": [{"fields": [{"type": "text", "label": "Current location"}], "buttons": [{"text": "Submit"}]}],
        }

        self.assertTrue(loop._manual_verification_requires_handoff(observation))

    def test_ai_browser_loop_page_blockers_detect_active_hcaptcha_controls(self) -> None:
        loop = AiBrowserApplyLoop()
        blockers = loop._page_blockers(
            [
                {
                    "url": "https://newassets.hcaptcha.com/captcha/v1/test/static/hcaptcha.html",
                    "visible": True,
                    "in_viewport": True,
                    "blockers": ["manual_verification"],
                    "fields": [{"type": "text", "label": "Current location"}],
                    "buttons": [{"text": "Verify"}, {"text": "Skip"}],
                    "validation_errors": [],
                }
            ],
            "",
        )

        self.assertIn("manual_verification", blockers)
        self.assertIn("interactive_verification", blockers)

    def test_ai_browser_loop_page_blockers_ignore_hidden_hcaptcha_controls(self) -> None:
        loop = AiBrowserApplyLoop()
        blockers = loop._page_blockers(
            [
                {
                    "url": "https://newassets.hcaptcha.com/captcha/v1/test/static/hcaptcha.html",
                    "visible": False,
                    "in_viewport": False,
                    "blockers": [],
                    "fields": [{"type": "text", "label": "Current location"}],
                    "buttons": [{"text": "Verify"}, {"text": "Skip"}],
                    "validation_errors": [],
                }
            ],
            "",
        )

        self.assertNotIn("manual_verification", blockers)
        self.assertNotIn("interactive_verification", blockers)

    def test_ai_browser_loop_page_blockers_allow_passive_hcaptcha_frame(self) -> None:
        loop = AiBrowserApplyLoop()
        blockers = loop._page_blockers(
            [
                {
                    "url": "https://newassets.hcaptcha.com/captcha/v1/test/static/hcaptcha.html",
                    "blockers": ["manual_verification"],
                    "fields": [{"type": "text", "label": "Current location"}],
                    "buttons": [{"text": "Submit Application"}],
                    "validation_errors": [],
                }
            ],
            "",
        )

        self.assertIn("manual_verification", blockers)
        self.assertNotIn("interactive_verification", blockers)

    def test_ai_browser_loop_mid_action_guard_hands_off_visible_interactive_verification(self) -> None:
        loop = AiBrowserApplyLoop()
        loop._observe = lambda page, *, detected_ats: {
            "blockers": ["manual_verification", "interactive_verification"],
            "frames": [
                {
                    "visible": True,
                    "fields": [{"type": "text", "label": "Current location"}],
                    "buttons": [{"text": "Verify"}],
                }
            ],
        }

        with self.assertRaises(ManualHandoffRequested):
            loop._raise_if_manual_blocker_now(object(), detected_ats="lever")

    def test_ai_browser_loop_does_not_retry_verification_error_without_manual_resume(self) -> None:
        loop = AiBrowserApplyLoop()
        loop._submit_attempts = 1

        self.assertFalse(
            loop._retry_submit_after_verification_error_if_ready(
                page=object(),
                observation={"blockers": ["verification_error"], "frames": []},
                resume_path=Path("/tmp/resume.pdf"),
                screenshot_path=Path("/tmp/screenshot.png"),
                auto_submit=True,
                steps=[],
            )
        )

    def test_ai_browser_loop_coerces_blank_compensation_to_negotiable(self) -> None:
        loop = AiBrowserApplyLoop()
        loop._current_applicant = ApplicantProfileConfig(compensation_expectation="0")
        target = {"type": "text", "label": "What is your desired salary for this position?"}

        self.assertEqual(
            loop._coerce_field_value(
                "I am open to discussing market-rate compensation based on the role and total package.",
                target,
            ),
            "Negotiable",
        )

    def test_ai_browser_loop_manual_verification_handoff_ignores_captcha_verify_buttons(self) -> None:
        loop = AiBrowserApplyLoop()
        observation = {
            "blockers": ["manual_verification"],
            "frames": [{"fields": [], "buttons": [{"text": "Verify"}]}],
        }

        self.assertTrue(loop._manual_verification_requires_handoff(observation))

    def test_ai_browser_loop_downgrades_actionable_manual_verification_for_planner(self) -> None:
        observation = {
            "blockers": ["manual_verification"],
            "frames": [
                {
                    "blockers": ["manual_verification"],
                    "fields": [{"type": "text", "label": "First Name"}],
                    "buttons": [{"text": "NEXT"}],
                }
            ],
        }

        cleaned = AiBrowserApplyLoop._downgrade_actionable_manual_verification(observation)

        self.assertEqual(cleaned["blockers"], [])
        self.assertEqual(cleaned["frames"][0]["blockers"], [])
        self.assertTrue(cleaned["manual_verification_present_but_actionable"])

    def test_ai_browser_loop_manual_handoff_waits_for_explicit_resume(self) -> None:
        loop = AiBrowserApplyLoop()
        page = _FakeManualPage()
        job = Job(id="job-1", project_id="test-project", company_name="Example", job_title="Engineer")
        choices = iter(["", "resume"])
        steps: list[str] = []

        loop._remove_activity_overlay = lambda page: None
        loop._show_manual_completion_overlay = lambda page, message, dock_only=False: None
        loop._manual_overlay_state = lambda page: "modal"
        loop._success_text = lambda page: ""
        loop._manual_overlay_choice = lambda page: next(choices)
        loop._remove_manual_overlay = lambda page: None
        loop._return_to_manual_resume_url = lambda page, steps: steps.append("returned_to_resume_url")
        loop._manual_blocker_cleared = lambda *args, **kwargs: self.fail(
            "manual handoff should not auto-resume without a user choice"
        )

        result = loop._await_manual_resolution(
            page=page,
            job=job,
            detected_ats="oracle_cloud",
            screenshot_path=Path("/tmp/manual.png"),
            steps=steps,
            cancel_checker=lambda: False,
            message="Manual step needed.",
        )

        self.assertIsNone(result)
        self.assertEqual(page.wait_count, 1)
        self.assertIn("User asked AI to resume after manual login or verification.", steps)
        self.assertIn("returned_to_resume_url", steps)

    def test_ai_browser_loop_restores_manual_dock_after_page_reload(self) -> None:
        loop = AiBrowserApplyLoop()
        page = _FakeManualPage()
        job = Job(id="job-1", project_id="test-project", company_name="Example", job_title="Engineer")
        choices = iter(["", "resume"])
        states = iter(["dock_missing"])
        show_calls: list[bool] = []
        steps: list[str] = []

        loop._remove_activity_overlay = lambda page: None
        loop._show_manual_completion_overlay = (
            lambda page, message, dock_only=False: show_calls.append(bool(dock_only))
        )
        loop._success_text = lambda page: ""
        loop._manual_overlay_choice = lambda page: next(choices)
        loop._manual_overlay_state = lambda page: next(states)
        loop._remove_manual_overlay = lambda page: None
        loop._return_to_manual_resume_url = lambda page, steps: None

        result = loop._await_manual_resolution(
            page=page,
            job=job,
            detected_ats="lever",
            screenshot_path=Path("/tmp/manual.png"),
            steps=steps,
            cancel_checker=lambda: False,
            message="Manual step needed.",
        )

        self.assertIsNone(result)
        self.assertEqual(show_calls, [False, True])
        self.assertIn("Restored the manual Resume AI bar after page navigation.", steps)

    def test_ai_browser_loop_manual_resume_keeps_current_application_form(self) -> None:
        loop = AiBrowserApplyLoop()
        page = _FakeResumeNavigationPage("https://jpmc.fa.oraclecloud.com/apply/section/2")
        loop._manual_resume_url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210742179"
        loop._current_page_looks_like_application_form = lambda page: True
        steps: list[str] = []

        loop._return_to_manual_resume_url(page, steps)

        self.assertEqual(page.goto_calls, [])
        self.assertIn("continuing without navigation", steps[-1])

    def test_ai_browser_loop_manual_resume_redirects_from_non_application_page(self) -> None:
        loop = AiBrowserApplyLoop()
        page = _FakeResumeNavigationPage("https://jpmc.fa.oraclecloud.com/profile")
        loop._manual_resume_url = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210742179"
        loop._current_page_looks_like_application_form = lambda page: False
        steps: list[str] = []

        loop._return_to_manual_resume_url(page, steps)

        self.assertEqual(page.goto_calls, [loop._manual_resume_url])
        self.assertIn("Returned to saved application URL", steps[-1])

    def test_ai_browser_loop_application_form_detection_rejects_email_only_auth_page(self) -> None:
        loop = AiBrowserApplyLoop()
        observation = {
            "blockers": [],
            "frames": [
                {
                    "fields": [{"type": "email", "label": "Email Address", "required": True}],
                    "buttons": [{"text": "NEXT"}],
                }
            ],
        }

        self.assertFalse(loop._observation_looks_like_application_form(observation, page_text="Email Address NEXT"))

    def test_ai_browser_loop_application_form_detection_accepts_personal_fields(self) -> None:
        loop = AiBrowserApplyLoop()
        observation = {
            "blockers": [],
            "frames": [
                {
                    "fields": [
                        {"type": "text", "label": "First Name", "required": True},
                        {"type": "text", "label": "Last Name", "required": True},
                        {"type": "email", "label": "Primary Email", "required": True},
                    ],
                    "buttons": [{"text": "NEXT"}],
                }
            ],
        }

        self.assertTrue(loop._observation_looks_like_application_form(observation, page_text="Job Application"))

    def test_ai_browser_loop_application_form_detection_accepts_yes_no_application_section(self) -> None:
        loop = AiBrowserApplyLoop()
        observation = {
            "blockers": [],
            "frames": [
                {
                    "fields": [],
                    "buttons": [
                        {"text": "Yes"},
                        {"text": "No"},
                        {"text": "Yes"},
                        {"text": "No"},
                        {"text": "NEXT"},
                    ],
                }
            ],
        }
        page_text = "Job Application. Are you legally authorized to work in the United States? Require sponsorship?"

        self.assertTrue(loop._observation_looks_like_application_form(observation, page_text=page_text))

    def test_ai_browser_loop_checkbox_select_uses_fallback_when_native_check_does_not_stick(self) -> None:
        loop = AiBrowserApplyLoop()
        locator = _FakeCheckableLocator(checked=False)
        frame = _FakeCheckableFrame(result=True)
        target = {
            "type": "checkbox",
            "tag": "input",
            "frame": frame,
            "selector": '[data-apply-agent-id="terms"]',
        }

        result = loop._select_target(None, locator, target, "yes")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["message"], "selected_checkbox")
        self.assertEqual(locator.check_calls, 1)
        self.assertEqual(frame.payload, {"selector": '[data-apply-agent-id="terms"]', "checked": True})

    def test_ai_browser_loop_does_not_apply_lever_consent_fixer_generically(self) -> None:
        loop = AiBrowserApplyLoop()

        self.assertFalse(
            loop._check_missing_required_consent_checkboxes(
                page=object(),
                observation={"frames": []},
                resume_path=Path("/tmp/resume.pdf"),
                screenshot_path=Path("/tmp/screenshot.png"),
                auto_submit=True,
                steps=[],
            )
        )

    def test_ai_browser_loop_does_not_apply_lever_ready_submit_generically(self) -> None:
        loop = AiBrowserApplyLoop()

        self.assertFalse(
            loop._submit_if_plan_says_ready(
                page=object(),
                observation={"frames": []},
                plan_message="Application form is complete with all required fields filled. Ready to submit.",
                resume_path=Path("/tmp/resume.pdf"),
                screenshot_path=Path("/tmp/screenshot.png"),
                auto_submit=True,
                steps=[],
            )
        )

    def test_lever_loop_does_not_check_submit_consent_from_observation_hook(self) -> None:
        loop = LeverAiBrowserApplyLoop()

        self.assertFalse(
            loop._check_missing_required_consent_checkboxes(
                page=object(),
                observation={"frames": []},
                resume_path=Path("/tmp/resume.pdf"),
                screenshot_path=Path("/tmp/screenshot.png"),
                auto_submit=True,
                steps=[],
            )
        )

    def test_lever_loop_identifies_missing_required_submit_consent_checkbox(self) -> None:
        target = {
            "kind": "field",
            "type": "checkbox",
            "label": "Submit Application",
            "required": True,
            "checked": False,
        }

        self.assertTrue(LeverAiBrowserApplyLoop._is_missing_required_consent_checkbox(target))

    def test_lever_loop_identifies_submit_consent_checkbox_even_without_required_flag(self) -> None:
        target = {
            "kind": "field",
            "type": "checkbox",
            "label": "Submit Application",
            "required": False,
            "checked": False,
        }

        self.assertTrue(LeverAiBrowserApplyLoop._is_missing_required_consent_checkbox(target))

    def test_lever_loop_does_not_treat_unrelated_checkbox_as_submit_consent(self) -> None:
        target = {
            "kind": "field",
            "type": "checkbox",
            "label": "Send me job alerts",
            "required": False,
            "checked": False,
        }

        self.assertFalse(LeverAiBrowserApplyLoop._is_missing_required_consent_checkbox(target))

    def test_lever_loop_ignores_already_checked_submit_consent_checkbox(self) -> None:
        target = {
            "kind": "field",
            "type": "checkbox",
            "label": "Submit Application",
            "required": True,
            "checked": True,
        }

        self.assertFalse(LeverAiBrowserApplyLoop._is_missing_required_consent_checkbox(target))

    def test_lever_loop_defers_model_selected_submit_consent_until_submit(self) -> None:
        loop = LeverAiBrowserApplyLoop()
        target = {
            "id": "frame_0:el_024",
            "kind": "field",
            "type": "checkbox",
            "label": "Submit Application",
            "required": True,
            "checked": False,
        }

        result = loop._execute_action(
            page=_FakePageWithFrames("https://jobs.lever.co/aledade/job/apply", []),
            action=ApplyAction(action="select", element_id="frame_0:el_024", value=True),
            targets={"frame_0:el_024": target},
            resume_path=Path("/tmp/resume.pdf"),
            screenshot_path=Path("/tmp/screenshot.png"),
            auto_submit=True,
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["message"], "lever_submit_consent_deferred_until_submit")

    def test_lever_loop_identifies_resume_upload_target(self) -> None:
        target = {
            "id": "frame_0:el_001",
            "kind": "field",
            "type": "file",
            "label": "Resume/CV * | Attach Resume/CV",
            "required": True,
        }

        self.assertTrue(LeverAiBrowserApplyLoop._is_resume_upload_target(target))

    def test_lever_loop_does_not_treat_cover_letter_upload_as_resume(self) -> None:
        target = {
            "id": "frame_0:el_002",
            "kind": "field",
            "type": "file",
            "label": "Cover Letter | Attach cover letter",
            "required": False,
        }

        self.assertFalse(LeverAiBrowserApplyLoop._is_resume_upload_target(target))

    def test_lever_loop_prefers_required_resume_upload_target(self) -> None:
        cover_letter = {
            "id": "frame_0:el_001",
            "kind": "field",
            "type": "file",
            "label": "Cover Letter",
            "required": False,
        }
        resume = {
            "id": "frame_0:el_002",
            "kind": "field",
            "type": "file",
            "label": "Resume/CV",
            "required": True,
        }

        self.assertIs(
            LeverAiBrowserApplyLoop._resume_upload_target(
                {
                    str(cover_letter["id"]): cover_letter,
                    str(resume["id"]): resume,
                }
            ),
            resume,
        )

    def test_lever_loop_retargets_submit_button_after_target_refresh(self) -> None:
        loop = LeverAiBrowserApplyLoop()

        action = loop._submit_action_for_targets(
            ApplyAction(action="submit_application", element_id="stale-button", reasoning="ready", confidence=0.9),
            {
                "frame_0:btn_006": {
                    "id": "frame_0:btn_006",
                    "kind": "button",
                    "tag": "button",
                    "text": "SUBMIT APPLICATION",
                    "label": "SUBMIT APPLICATION",
                    "disabled": False,
                }
            },
        )

        self.assertEqual(action.element_id, "frame_0:btn_006")
        self.assertEqual(action.reasoning, "ready")

    def test_lever_loop_plan_ready_message_allows_deterministic_submit(self) -> None:
        self.assertTrue(
            LeverAiBrowserApplyLoop._plan_message_indicates_ready_to_submit(
                "Application form is complete with all required fields filled. Ready to submit."
            )
        )

    def test_lever_loop_plan_not_ready_message_does_not_submit(self) -> None:
        self.assertFalse(
            LeverAiBrowserApplyLoop._plan_message_indicates_ready_to_submit(
                "Application form is not ready to submit because required fields remain."
            )
        )

    def test_ai_browser_loop_button_click_fallback_uses_observed_ordinal_and_text(self) -> None:
        loop = AiBrowserApplyLoop()
        frame = _FakeButtonFallbackFrame(result=True)
        target = {"id": "frame_0:btn_006", "kind": "button", "text": "No", "frame": frame}

        self.assertTrue(loop._click_observed_button_fallback(target))
        self.assertEqual(frame.payload, {"ordinal": 5, "text": "no"})

    def test_oracle_terms_helper_is_oracle_scoped(self) -> None:
        self.assertTrue(is_oracle_cloud_page(_FakePageWithFrames("https://jpmc.fa.oraclecloud.com/apply", [])))
        self.assertTrue(
            is_oracle_cloud_page(
                _FakePageWithFrames(
                    "https://example.com/apply",
                    [_FakeFrameWithUrl("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience")],
                )
            )
        )
        self.assertFalse(is_oracle_cloud_page(_FakePageWithFrames("https://boards.greenhouse.io/acme/jobs/1", [])))

    def test_ai_browser_loop_masks_password_field_current_value(self) -> None:
        loop = AiBrowserApplyLoop()
        field = ApplyField(
            element_id="password",
            frame_id="frame_0",
            tag="input",
            type="password",
            label="Password",
            selector="[data-apply-agent-id='password']",
        )

        class FakeLocator:
            def input_value(self, timeout=500):
                return "SuperSecret123!"

            def evaluate(self, script):
                return False

        class FakeFrame:
            def locator(self, selector):
                return type("LocatorHandle", (), {"first": FakeLocator()})()

        value_info = loop._field_value_info(FakeFrame(), field)

        self.assertTrue(value_info["has_value"])
        self.assertEqual(value_info["current_value"], "[password set]")
        self.assertNotIn("SuperSecret", value_info["current_value"])

    def test_ai_browser_loop_uses_password_placeholder_for_keychain_password(self) -> None:
        from job_apps_system.services.apply_site_sessions import ApplySiteCredential

        credential = ApplySiteCredential(
            site_key="workday",
            email="candidate@example.com",
            password="GeneratedPassword123!",
            created_at="now",
            updated_at="now",
        )
        loop = AiBrowserApplyLoop()
        target = {"type": "password", "label": "Password"}

        self.assertEqual(loop._resolve_action_value("__APPLY_SITE_PASSWORD__", target, credential), credential.password)

    def test_ai_browser_loop_yields_dice_register_page_to_adapter(self) -> None:
        loop = AiBrowserApplyLoop()
        page = type("Page", (), {"url": "https://www.dice.com/register"})()
        observation = {
            "detected_ats": "dice_profile",
            "page_url": "https://www.dice.com/register",
            "frames": [{"frame_id": "frame_0", "url": "https://www.dice.com/register", "fields": []}],
            "blockers": [],
        }

        self.assertTrue(loop._should_yield_dice_profile_detour(page, observation))

    def test_ai_browser_loop_keeps_dice_register_form_when_fields_exist(self) -> None:
        loop = AiBrowserApplyLoop()
        page = type("Page", (), {"url": "https://www.dice.com/register"})()
        observation = {
            "detected_ats": "dice_profile",
            "page_url": "https://www.dice.com/register",
            "frames": [
                {
                    "frame_id": "frame_0",
                    "url": "https://www.dice.com/register",
                    "fields": [{"type": "email", "label": "Email", "required": True}],
                }
            ],
            "blockers": [],
        }

        self.assertFalse(loop._should_yield_dice_profile_detour(page, observation))

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

    def test_ai_browser_loop_logs_target_with_blank_label(self) -> None:
        loop = AiBrowserApplyLoop()

        loop._log_action(
            ApplyAction(action="select", element_id="frame_0:el_002", value="Texas"),
            "failed",
            "select_option_not_found",
            {"kind": "field", "label": None, "text": None},
        )

        self.assertEqual(loop._action_log[-1]["target_label"], "")

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

class _FakeManualPage:
    url = "https://example.test/login"

    def __init__(self) -> None:
        self.wait_count = 0
        self.screenshot_count = 0

    def is_closed(self) -> bool:
        return False

    def screenshot(self, path: str, full_page: bool = False) -> None:
        self.screenshot_count += 1

    def wait_for_timeout(self, timeout: int) -> None:
        self.wait_count += 1


class _FakeResumeNavigationPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.goto_calls: list[str] = []
        self.load_state_calls: list[str] = []
        self.wait_count = 0

    def is_closed(self) -> bool:
        return False

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self.goto_calls.append(url)
        self.url = url

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.load_state_calls.append(state)

    def wait_for_timeout(self, timeout: int) -> None:
        self.wait_count += 1


class _FakeCheckableFrame:
    def __init__(self, *, result: bool) -> None:
        self.result = result
        self.payload: dict | None = None

    def evaluate(self, script: str, payload: dict) -> bool:
        self.payload = payload
        return self.result


class _FakeCheckableLocator:
    def __init__(self, *, checked: bool) -> None:
        self.checked = checked
        self.check_calls = 0
        self.uncheck_calls = 0

    def check(self, timeout: int = 0, force: bool = False) -> None:
        self.check_calls += 1

    def uncheck(self, timeout: int = 0, force: bool = False) -> None:
        self.uncheck_calls += 1

    def is_checked(self, timeout: int = 0) -> bool:
        return self.checked


class _FakeButtonFallbackFrame:
    def __init__(self, *, result: bool) -> None:
        self.result = result
        self.payload: dict | None = None

    def evaluate(self, script: str, payload: dict) -> bool:
        self.payload = payload
        return self.result


class _FakeFrameWithUrl:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePageWithFrames:
    def __init__(self, url: str, frames: list[_FakeFrameWithUrl]) -> None:
        self.url = url
        self.frames = frames


class _FakeLocator:
    def __init__(self) -> None:
        self.filled_value: str | None = None

    def fill(self, value: str, timeout: int = 0) -> None:
        self.filled_value = value


if __name__ == "__main__":
    unittest.main()
