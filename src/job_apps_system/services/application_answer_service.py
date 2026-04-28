from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import select

from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.unanswered_questions import UnansweredApplicationQuestion
from job_apps_system.integrations.llm.anthropic_client import AnthropicClient
from job_apps_system.schemas.apply import ApplyField
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
