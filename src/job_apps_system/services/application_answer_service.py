from __future__ import annotations

import re

from job_apps_system.config.models import ApplicantProfileConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.llm.anthropic_client import AnthropicClient
from job_apps_system.services.setup_config import load_setup_config


ANSWER_SYSTEM_PROMPT = """You write accurate, concise job application form answers for a candidate.
Use only the provided candidate profile, resume content, and job description.
Do not invent credentials, employers, certifications, degrees, or legal facts.
For open-ended questions, answer in first person with a professional tone.
Return only the answer text."""


class ApplicationAnswerService:
    def __init__(self, session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._client = AnthropicClient(session=session)
        self._model = self._config.models.anthropic_model

    def generate_custom_answer(
        self,
        *,
        question: str,
        applicant: ApplicantProfileConfig,
        job: Job,
        constraints: str = "",
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
        return _clean_answer(response)

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
    return answer.strip('"').strip()
