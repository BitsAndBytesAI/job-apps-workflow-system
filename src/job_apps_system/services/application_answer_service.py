from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from sqlalchemy import select

from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.unanswered_questions import UnansweredApplicationQuestion
from job_apps_system.integrations.llm.anthropic_client import AnthropicClient
from job_apps_system.schemas.apply import ApplyActionPlan, ApplyField
from job_apps_system.services.setup_config import load_setup_config


ANSWER_SYSTEM_PROMPT = """You write accurate, concise job application form answers for a candidate.
Use only the provided candidate profile, resume content, and job description.
Do not invent credentials, employers, certifications, degrees, or legal facts.
For open-ended questions, answer in first person with a professional tone.
Never explain that information is missing, unavailable, or cannot be answered.
If the provided information is not sufficient for an accurate answer, return an empty response.
Return only the answer text."""

YES_NO_SYSTEM_PROMPT = """You answer a job application Yes/No question for a candidate.
Use only the provided candidate profile, resume content, and job description.
Do not invent legal, employment, or experience facts.
Return only one of: Yes, No, or blank.
If the provided information is not sufficient for a safe answer, return blank."""

BROWSER_ACTION_SYSTEM_PROMPT = """You control a bounded browser automation loop for filling a job application.
Return only valid JSON matching the requested contract.

Rules:
- Use only observed element_id values from the current page state.
- Choose only allowed actions: fill, select, click, upload_resume, scroll, wait, screenshot, submit_application, stop_for_manual, needs_review.
- The executor validates every action. Do not invent selectors, JavaScript, file paths, credentials, tokens, or external URLs.
- You may fill normal and sensitive job-application fields using setup profile data, resume/job context, and reasonable inference.
- Do not make obviously false claims.
- Preserve existing meaningful values unless they are invalid, placeholder text, or the user explicitly needs a corrected value.
- For voluntary self-identification fields such as gender, disability, race/ethnicity, veteran status, military status, or military spouse/domestic partner, prefer a decline/prefer-not-to-answer option when available unless the setup profile provides a specific answer. Do not type name, email, phone, or other contact data into these controls.
- Use click for pre-application navigation controls like "Apply", "Apply Now", "Start Application", or "Apply on company website".
- submit_application means final form submission. Use it only when the application fields are complete and the page appears ready to submit.
- If auth_context.credentials_available is true, automate ordinary login or account creation with the provided email and placeholders.
- For password and confirm-password fields, use auth_context.password_placeholder exactly; never invent, expose, or print a password.
- Prefer email/password account creation or sign-in over social login buttons like Google, Apple, LinkedIn, or SSO.
- For ordinary account creation/application terms, privacy, and communication checkboxes, use select when they are required to proceed.
- If manual verification is the only thing blocking progress, use stop_for_manual. If normal application fields or buttons are still actionable, continue filling the application instead of stopping just because a CAPTCHA frame is present.
- If the page requires unavailable credentials, unavailable files, payment/banking details, MFA, or password reset, use stop_for_manual.
- If apply_auto_submit is false and the page is ready to submit, use needs_review instead of submit_application.
"""


DECLINE_SELF_IDENTIFY_CHOICES = [
    "Decline To Self Identify",
    "Prefer not to answer",
    "I don't wish to answer",
    "I do not want to answer",
]


class ApplicationAnswerService:
    def __init__(self, session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._client = AnthropicClient(session=session)
        self._model = self._config.models.anthropic_model

    @property
    def resume_text(self) -> str:
        return self._config.project_resume.extracted_text or ""

    def generate_custom_answer(
        self,
        *,
        question: str,
        applicant: ApplicantProfileConfig,
        job: Job,
        constraints: str = "",
        ats_type: str = "unknown",
        field_type: str | None = None,
        required: bool = False,
    ) -> str:
        response = self._client.generate_text(
            model=self._model,
            system_prompt=ANSWER_SYSTEM_PROMPT,
            user_prompt=self._build_prompt(
                question=question,
                applicant=applicant,
                job=job,
                constraints=constraints,
            ),
            max_tokens=700,
            temperature=0.2,
        )
        answer = _clean_answer(response)
        if not answer:
            self.record_unanswered_question(
                job=job,
                question=question,
                ats_type=ats_type,
                field_type=field_type,
                required=required,
                reason="llm_empty",
            )
        return answer

    def generate_yes_no_answer(
        self,
        *,
        question: str,
        applicant: ApplicantProfileConfig,
        job: Job,
        ats_type: str = "unknown",
        required: bool = False,
    ) -> bool | None:
        response = self._client.generate_text(
            model=self._model,
            system_prompt=YES_NO_SYSTEM_PROMPT,
            user_prompt=self._build_prompt(
                question=question,
                applicant=applicant,
                job=job,
                constraints="Return only Yes or No. If there is not enough information for a safe answer, return blank.",
            ),
            max_tokens=20,
            temperature=0.0,
        )
        answer = _clean_yes_no_answer(response)
        if answer is None:
            self.record_unanswered_question(
                job=job,
                question=question,
                ats_type=ats_type,
                field_type="yes_no",
                required=required,
                reason="binary_llm_empty",
            )
        return answer

    def generate_browser_action_plan(
        self,
        *,
        observation: dict,
        applicant: ApplicantProfileConfig,
        job: Job,
        auto_submit: bool,
        auth_context: dict | None = None,
        recent_actions: list[dict] | None = None,
    ) -> ApplyActionPlan:
        response = self._client.generate_json(
            model=self._model,
            system_prompt=BROWSER_ACTION_SYSTEM_PROMPT,
            user_prompt=self._build_browser_action_prompt(
                observation=observation,
                applicant=applicant,
                job=job,
                auto_submit=auto_submit,
                auth_context=auth_context or {},
                recent_actions=recent_actions or [],
            ),
            max_tokens=1800,
            temperature=0.0,
        )
        payload = _parse_json_object(response)
        return ApplyActionPlan.model_validate(payload)

    def record_unanswered_field(
        self,
        *,
        job: Job,
        field: ApplyField,
        ats_type: str,
        reason: str,
    ) -> None:
        self.record_unanswered_question(
            job=job,
            question=field.label,
            ats_type=ats_type,
            field_type=field.type or field.tag,
            required=field.required,
            reason=reason,
        )

    def record_unanswered_question(
        self,
        *,
        job: Job,
        question: str,
        ats_type: str,
        reason: str,
        field_type: str | None = None,
        required: bool = False,
    ) -> None:
        question_text = _normalize_question_text(question)
        if not question_text:
            return

        now = datetime.now(timezone.utc)
        row = self._session.scalar(
            select(UnansweredApplicationQuestion).where(
                UnansweredApplicationQuestion.project_id == self._config.app.project_id,
                UnansweredApplicationQuestion.job_id == job.id,
                UnansweredApplicationQuestion.question_text == question_text,
            )
        )
        if row is None:
            row = UnansweredApplicationQuestion(
                project_id=self._config.app.project_id,
                job_id=job.id,
                company_name=job.company_name,
                job_title=job.job_title,
                ats_type=ats_type,
                question_text=question_text,
                field_type=(field_type or "").strip() or None,
                required=required,
                reason=reason,
                first_seen_at=now,
                last_seen_at=now,
                occurrence_count=1,
            )
            self._session.add(row)
        else:
            row.company_name = job.company_name
            row.job_title = job.job_title
            row.ats_type = ats_type or row.ats_type
            row.field_type = (field_type or "").strip() or row.field_type
            row.required = row.required or required
            row.reason = reason
            row.last_seen_at = now
            row.occurrence_count = (row.occurrence_count or 0) + 1
        self._session.flush()

    def _build_prompt(
        self,
        *,
        question: str,
        applicant: ApplicantProfileConfig,
        job: Job,
        constraints: str,
    ) -> str:
        return f"""Application question:
{question}

Answer constraints:
{constraints or "Keep the answer direct and useful. If the field appears short, keep it under 250 characters."}

Candidate profile:
- Legal name: {applicant.legal_name}
- Current title: {applicant.current_title}
- Current company: {applicant.current_company}
- Years of experience: {applicant.years_of_experience}
- Location: {applicant.location_summary}
- LinkedIn: {applicant.linkedin_url}
- Portfolio: {applicant.portfolio_url}
- GitHub: {applicant.github_url}
- Work authorized in the US: {"Yes" if applicant.work_authorized_us else "No"}
- Requires sponsorship: {"Yes" if applicant.requires_sponsorship else "No"}
- Compensation expectation: {applicant.compensation_expectation}
- Programming languages and years: {applicant.programming_languages_years}
- Favorite AI tool: {applicant.favorite_ai_tool}
- Favorite AI tool usage: {applicant.favorite_ai_tool_usage}
- Company value/story example: {applicant.company_value_example}
- Why interested guidance: {applicant.why_interested_guidance}
- Additional information guidance: {applicant.additional_info_guidance}
- User answer guidance: {applicant.custom_answer_guidance}

Job:
- Company: {job.company_name or ""}
- Title: {job.job_title or ""}
- Description:
{job.job_description or ""}

Base resume content:
{self._config.project_resume.extracted_text or ""}
"""

    def _build_browser_action_prompt(
        self,
        *,
        observation: dict,
        applicant: ApplicantProfileConfig,
        job: Job,
        auto_submit: bool,
        auth_context: dict,
        recent_actions: list[dict],
    ) -> str:
        applicant_context = {
            "legal_name": applicant.legal_name,
            "preferred_name": applicant.preferred_name,
            "email": applicant.email,
            "phone": applicant.phone,
            "linkedin_url": applicant.linkedin_url,
            "portfolio_url": applicant.portfolio_url,
            "github_url": applicant.github_url,
            "current_company": applicant.current_company,
            "current_title": applicant.current_title,
            "years_of_experience": applicant.years_of_experience,
            "location": applicant.location_summary,
            "country": applicant.country,
            "work_authorized_us": applicant.work_authorized_us,
            "requires_sponsorship": applicant.requires_sponsorship,
            "compensation_expectation": applicant.compensation_expectation,
            "programming_languages_years": applicant.programming_languages_years,
            "favorite_ai_tool": applicant.favorite_ai_tool,
            "favorite_ai_tool_usage": applicant.favorite_ai_tool_usage,
            "company_value_example": applicant.company_value_example,
            "why_interested_guidance": applicant.why_interested_guidance,
            "additional_info_guidance": applicant.additional_info_guidance,
            "sms_consent": applicant.sms_consent,
            "custom_answer_guidance": applicant.custom_answer_guidance,
        }
        job_context = {
            "company": job.company_name or "",
            "title": job.job_title or "",
            "description": job.job_description or "",
            "apply_url": job.apply_url or "",
        }
        contract = {
            "confidence": 0.0,
            "terminal": False,
            "needs_manual": False,
            "needs_review": False,
            "done": False,
            "summary": "",
            "error": None,
            "actions": [
                {
                    "action": "fill",
                    "element_id": "observed_id",
                    "value": "answer text",
                    "reasoning": "brief reason",
                    "confidence": 0.0,
                }
            ],
        }
        return f"""Current browser observation:
{json.dumps(observation, ensure_ascii=False, indent=2)}

Auto-submit enabled:
{json.dumps(auto_submit)}

Auth context:
{json.dumps(auth_context, ensure_ascii=False, indent=2)}

Recent action history:
{json.dumps(recent_actions[-12:], ensure_ascii=False, indent=2)}

Candidate profile:
{json.dumps(applicant_context, ensure_ascii=False, indent=2)}

Job:
{json.dumps(job_context, ensure_ascii=False, indent=2)}

Base resume content:
{self._config.project_resume.extracted_text or ""}

Return JSON only in this contract:
{json.dumps(contract, ensure_ascii=False, indent=2)}
"""


def infer_structured_yes_no_answer(question: str, applicant: ApplicantProfileConfig) -> bool | None:
    label = _normalized_question(question)
    if not label:
        return None

    if any(
        token in label
        for token in (
            "at least 18",
            "18 years of age",
            "18 years old",
            "over 18",
            "older than 18",
            "age of majority",
        )
    ):
        return True
    if (
        any(token in label for token in ("legal right to work in the us", "authorized to work", "legally authorized"))
        and "sponsor" not in label
    ):
        return applicant.work_authorized_us
    if any(
        token in label
        for token in (
            "require sponsorship",
            "need sponsorship",
            "immigration sponsorship",
            "visa sponsorship",
            "employment-based sponsorship",
            "immigration-related support or sponsorship",
        )
    ):
        return applicant.requires_sponsorship
    if "family members" in label and "employed by" in label:
        return False
    if "close personal relationship" in label and "employed by" in label:
        return False
    if "have you been employed by" in label and "before" in label:
        return False
    if "worked for" in label and "before" in label:
        return False
    if "sms" in label or "text message" in label:
        return applicant.sms_consent
    return None


def infer_structured_choice_candidates(question: str, applicant: ApplicantProfileConfig) -> list[str]:
    label = _normalized_question(question)
    if not label:
        return []

    yes_no = infer_structured_yes_no_answer(question, applicant)
    if yes_no is not None:
        return ["Yes" if yes_no else "No"]

    if "how did you hear about" in label:
        return [
            "LinkedIn job post",
            "LinkedIn company page",
            "A recruiter contacted me (LinkedIn message)",
        ]
    if "how familiar were you" in label:
        return [
            "I learned about Upstart through this job posting or from a recruiter",
            "I had heard of Upstart, but didn’t know much",
        ]
    if "current location" in label:
        if "canada" in " ".join(applicant.country.lower().split()) or "canada" in " ".join(applicant.location_summary.lower().split()):
            return ["Canada (outside of Quebec)"]
        return ["United States"]
    if label.startswith("gender"):
        return DECLINE_SELF_IDENTIFY_CHOICES
    if "hispanic/latino" in label or "hispanic latino" in label:
        return DECLINE_SELF_IDENTIFY_CHOICES
    if "military spouse" in label or "domestic partner" in label:
        return ["No", "Not Applicable", *DECLINE_SELF_IDENTIFY_CHOICES]
    if "military status" in label:
        return [
            "I do not wish to answer",
            "I don't wish to answer",
            "Not a Veteran",
            "No Military Service",
            *DECLINE_SELF_IDENTIFY_CHOICES,
        ]
    if "veteran status" in label:
        return DECLINE_SELF_IDENTIFY_CHOICES
    if "disability status" in label:
        return DECLINE_SELF_IDENTIFY_CHOICES
    return []


def _clean_answer(value: str) -> str:
    answer = value.strip()
    answer = re.sub(r"^```(?:text|markdown)?", "", answer, flags=re.IGNORECASE).strip()
    answer = re.sub(r"```$", "", answer).strip()
    answer = answer.strip('"').strip()
    lowered = answer.lower()
    refusal_markers = (
        "i cannot answer",
        "i can't answer",
        "i do not have enough information",
        "i don't have enough information",
        "insufficient information",
        "not enough information",
        "cannot determine",
        "unable to answer",
        "this box is asking",
    )
    if any(marker in lowered for marker in refusal_markers):
        return ""
    return answer


def _parse_json_object(value: str) -> dict:
    candidate = value.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if match:
        candidate = match.group(0)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("Application browser action plan was not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Application browser action plan JSON must be an object.")
    return payload


def _normalized_question(question: str) -> str:
    return " ".join(_normalize_question_text(question).lower().split())


def _normalize_question_text(question: str) -> str:
    parts = []
    seen: set[str] = set()
    for part in (question or "").split("|"):
        cleaned = " ".join(part.split()).strip()
        lowered = cleaned.lower()
        if not cleaned:
            continue
        if lowered in {
            "type here...",
            "start typing...",
            "hello@example.com...",
            "upload file",
            "or drag and drop here",
        }:
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        parts.append(cleaned)
    if not parts:
        return ""
    if len(parts) >= 2 and parts[0].lower() == parts[-1].lower():
        return parts[0]
    return " | ".join(parts)


def _clean_yes_no_answer(value: str) -> bool | None:
    answer = _clean_answer(value)
    normalized = answer.strip().lower()
    if normalized.startswith("yes"):
        return True
    if normalized.startswith("no"):
        return False
    return None
