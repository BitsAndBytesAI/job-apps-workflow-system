from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.llm.anthropic_client import AnthropicClient
from job_apps_system.integrations.llm.prompts.outreach import (
    OUTREACH_PROMPT_TEMPLATE,
    OUTREACH_SYSTEM_PROMPT,
)
from job_apps_system.services.setup_config import load_setup_config


OUTREACH_MODEL = "claude-sonnet-4-5-20250929"
_JOB_DESCRIPTION_CHAR_LIMIT = 6000


class OutreachGenerationAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._client = AnthropicClient(session=session)
        self._model = OUTREACH_MODEL

    def generate_for_job(self, job: Job) -> dict[str, str]:
        applicant = self._config.applicant
        prompt = OUTREACH_PROMPT_TEMPLATE.format(
            applicant_name=applicant.preferred_name or applicant.legal_name or "Candidate",
            applicant_current_title=applicant.current_title or "",
            applicant_current_company=applicant.current_company or "",
            applicant_years=applicant.years_of_experience or "",
            applicant_highlights=self._applicant_highlights(),
            job_title=job.job_title or "",
            company_name=job.company_name or "",
            job_description=(job.job_description or "")[:_JOB_DESCRIPTION_CHAR_LIMIT],
        )

        raw = self._client.generate_json(
            model=self._model,
            system_prompt=OUTREACH_SYSTEM_PROMPT,
            user_prompt=prompt,
            max_tokens=900,
            temperature=0.4,
        )
        parsed = _parse_outreach_json(raw)
        return {
            "subject": (parsed.get("subject") or "").strip(),
            "body": (parsed.get("body") or "").strip(),
        }

    def _applicant_highlights(self) -> str:
        applicant = self._config.applicant
        resume_text = (self._config.project_resume.extracted_text or "").strip()
        if resume_text:
            return resume_text[:2000]
        # Fallback to structured profile fields when resume text isn't available.
        parts = [
            applicant.programming_languages_years,
            applicant.favorite_ai_tool_usage,
            applicant.company_value_example,
        ]
        return " ".join(part for part in parts if part)


def _parse_outreach_json(raw: str) -> dict[str, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Outreach generation returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Outreach generation must return a JSON object.")
    return data
