from __future__ import annotations

import json
import re
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.llm.anthropic_client import AnthropicClient
from job_apps_system.schemas.scoring import JobScoringSummary, ScoredJobSchema
from job_apps_system.services.setup_config import load_setup_config


SCORING_SYSTEM_PROMPT = (
    "You are a rigorous job-to-resume scoring agent. Score candidate-job fit only from the provided "
    "candidate data and job description. Use the rubric exactly, cite evidence for every dimension, "
    "and return only valid JSON."
)
SCORING_MODEL = "claude-sonnet-4-5-20250929"
SCORING_BATCH_SIZE = 10
SCORING_BATCH_WAIT_SECONDS = 90
SCORING_DEFAULT_THRESHOLD = 820

SCORING_RUBRIC = """Job ↔ Resume Match Rubric

Score each dimension 0.0–10.0, multiply by the weight, and sum. Apply modifiers last. Final overall score = 0–1000.00.

Core dimensions
1. Required hard-skill coverage (weight 18)
2. Skill recency (weight 8)
3. Skill depth (weight 10)
4. Years of experience vs. JD ask (weight 8)
5. Seniority/scope alignment (weight 10)
6. Domain/industry match (weight 8)
7. Stack specificity (weight 8)
8. Role-shape match (weight 6)
9. Trajectory fit (weight 6)
10. Company-stage match (weight 4)
11. Mission/product affinity signal (weight 6)
12. Logistics fit (weight 4)
13. Differentiator presence (weight 4)

Anchor discipline
- Use one decimal place on every dimension score.
- Cite exact evidence from both resume and JD for every dimension.
- If a dimension cannot be evaluated from the inputs, mark it N/A and redistribute that weight proportionally across the remaining dimensions instead of defaulting to 5.
- Calibration: 950+ means extremely strong fit, 500 is a coin flip, 200 is a polite rejection.

Modifiers
- Hard disqualifier present: set overall below 200 and do not score further.
- Title/level inflation risk: -15 to -30
- Domain-transfer evidence: +10 to +25
- Quantified impact in JD's target area: +5 to +20
- Stale-but-deep core skill: -10 to -20
- Over-specialization mismatch: -10 to -25
- Cultural/working-style signals: ±5

Output contract
{
  "overall": 0-1000.00,
  "verdict": "strong | plausible | stretch | mismatch | disqualified",
  "dimensions": [
    {
      "name": "string",
      "score": 0.0,
      "weight": 0,
      "evidence_resume": "exact resume evidence",
      "evidence_jd": "exact job description evidence"
    }
  ],
  "modifiers_applied": [
    {
      "name": "string",
      "delta": 0,
      "reason": "string"
    }
  ],
  "top_strengths": ["string", "string", "string"],
  "top_gaps": ["string", "string", "string"],
  "single_sentence_summary": "string"
}

Return JSON only. Do not add prose before or after the JSON."""


class JobScoringAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._client = AnthropicClient(session=session)
        self._model = SCORING_MODEL

    def run(
        self,
        *,
        limit: int | None = None,
        job_ids: list[str] | None = None,
        step_reporter=None,
        cancel_checker=None,
    ) -> JobScoringSummary:
        pending_jobs = self._load_pending_jobs(limit=limit, job_ids=job_ids or [])
        self._report_step(
            step_reporter,
            "Select pending jobs",
            "completed",
            f"Found {len(pending_jobs)} job(s) without a score.",
        )

        if not pending_jobs:
            return JobScoringSummary(
                ok=True,
                cancelled=False,
                message="No jobs needed scoring.",
                model=self._model,
                pending_jobs=0,
                attempted_count=0,
                scored_count=0,
                failed_count=0,
            )

        scored_jobs: list[ScoredJobSchema] = []
        failed_count = 0
        self._report_step(step_reporter, "Previous Job", "running", f"Scoring {len(pending_jobs)} job(s) with Anthropic.")

        attempted_count = 0
        total_jobs = len(pending_jobs)
        for batch_start in range(0, total_jobs, SCORING_BATCH_SIZE):
            batch_jobs = pending_jobs[batch_start : batch_start + SCORING_BATCH_SIZE]
            batch_number = (batch_start // SCORING_BATCH_SIZE) + 1
            batch_total = (total_jobs + SCORING_BATCH_SIZE - 1) // SCORING_BATCH_SIZE

            self._report_step(
                step_reporter,
                "Previous Job",
                "running",
                f"Scoring batch {batch_number}/{batch_total} ({len(batch_jobs)} job(s)).",
            )

            for job in batch_jobs:
                attempted_count += 1
                if self._is_cancelled(cancel_checker):
                    self._report_step(
                        step_reporter,
                        "Previous Job",
                        "completed",
                        f"Cancellation requested. Stopped after scoring {len(scored_jobs)} of {len(pending_jobs)} job(s).",
                    )
                    return JobScoringSummary(
                        ok=False,
                        cancelled=True,
                        message="Scoring agent cancelled.",
                        model=self._model,
                        pending_jobs=len(pending_jobs),
                        attempted_count=attempted_count - 1,
                        scored_count=len(scored_jobs),
                        failed_count=failed_count,
                        scored_jobs=scored_jobs,
                    )
                self._report_step(
                    step_reporter,
                    "Previous Job",
                    "running",
                    f"Scoring {attempted_count}/{len(pending_jobs)}: {job.company_name or 'Unknown company'} — {job.job_title or 'Untitled role'}.",
                )
                try:
                    response_text = self._client.generate_text(
                        model=self._model,
                        system_prompt=SCORING_SYSTEM_PROMPT,
                        user_prompt=self._build_user_prompt(job),
                        max_tokens=1200,
                        temperature=0,
                    )
                    score = self._parse_score(response_text)
                    job.score = score
                    self._session.commit()
                    scored_jobs.append(
                        ScoredJobSchema(
                            job_id=job.id,
                            company_name=job.company_name,
                            job_title=job.job_title,
                            score=score,
                            model=self._model,
                        )
                    )
                    self._report_step(
                        step_reporter,
                        "Previous Job",
                        "running",
                        f"Scored {attempted_count}/{len(pending_jobs)}: {job.company_name or 'Unknown company'} — {job.job_title or 'Untitled role'} = {score}.",
                    )
                except Exception as exc:
                    failed_count += 1
                    self._session.rollback()
                    self._report_step(
                        step_reporter,
                        "Previous Job",
                        "running",
                        f"Failed {attempted_count}/{len(pending_jobs)}: {job.company_name or 'Unknown company'} — {exc}",
                    )

            remaining_jobs = total_jobs - attempted_count
            if remaining_jobs > 0:
                wait_result = self._wait_between_batches(
                    cancel_checker=cancel_checker,
                    step_reporter=step_reporter,
                    remaining_jobs=remaining_jobs,
                    batch_number=batch_number,
                    batch_total=batch_total,
                )
                if wait_result.cancelled:
                    return JobScoringSummary(
                        ok=False,
                        cancelled=True,
                        message="Scoring agent cancelled.",
                        model=self._model,
                        pending_jobs=len(pending_jobs),
                        attempted_count=attempted_count,
                        scored_count=len(scored_jobs),
                        failed_count=failed_count,
                        scored_jobs=scored_jobs,
                    )

        self._report_step(
            step_reporter,
            "Previous Job",
            "completed",
            f"Scored {len(scored_jobs)} job(s); {failed_count} failed.",
        )

        return JobScoringSummary(
            ok=failed_count == 0 or bool(scored_jobs),
            cancelled=False,
            message="Job scoring completed." if scored_jobs or not failed_count else "Job scoring failed.",
            model=self._model,
            pending_jobs=len(pending_jobs),
            attempted_count=attempted_count,
            scored_count=len(scored_jobs),
            failed_count=failed_count,
            scored_jobs=scored_jobs,
        )

    def _wait_between_batches(self, *, cancel_checker, step_reporter, remaining_jobs: int, batch_number: int, batch_total: int):
        self._report_step(
            step_reporter,
            "Throttle wait",
            "running",
            f"Waiting {SCORING_BATCH_WAIT_SECONDS} seconds before batch {batch_number + 1}/{batch_total}. {remaining_jobs} job(s) remaining.",
        )
        for second in range(SCORING_BATCH_WAIT_SECONDS):
            if self._is_cancelled(cancel_checker):
                self._report_step(
                    step_reporter,
                    "Throttle wait",
                    "completed",
                    "Cancellation requested during scoring throttle wait.",
                )
                class WaitResult:
                    cancelled = True

                return WaitResult()
            time.sleep(1)

        self._report_step(
            step_reporter,
            "Throttle wait",
            "completed",
            f"Throttle wait complete. Starting next scoring batch.",
        )
        class WaitResult:
            cancelled = False

        return WaitResult()

    def _load_pending_jobs(self, *, limit: int | None, job_ids: list[str]) -> list[Job]:
        query = select(Job).where(Job.project_id == self._project_id)
        query = query.where((Job.intake_decision.is_(None)) | (Job.intake_decision == "accepted"))
        if job_ids:
            query = query.where(Job.id.in_(job_ids))
        else:
            query = query.where(Job.score.is_(None))

        rows = self._session.scalars(
            query.order_by(Job.created_time.asc().nullslast(), Job.id.asc())
        ).all()

        filtered = [row for row in rows if row.job_description]
        if limit is not None:
            return filtered[:limit]
        return filtered

    def _build_user_prompt(self, job: Job) -> str:
        base_resume_text = (self._config.project_resume.extracted_text or "").strip()
        if not base_resume_text:
            raise ValueError("Base resume extracted text is missing. Save the base resume before scoring jobs.")

        applicant = self._config.applicant
        candidate_context = {
            "target_role": self._config.app.job_role or "",
            "current_company": applicant.current_company,
            "current_title": applicant.current_title,
            "years_of_experience": applicant.years_of_experience,
            "location": applicant.location_summary,
            "country": applicant.country,
            "work_authorized_us": applicant.work_authorized_us,
            "requires_sponsorship": applicant.requires_sponsorship,
            "compensation_expectation": applicant.compensation_expectation,
            "selected_job_sites": self._config.app.selected_job_sites,
        }

        return f"""Use the rubric below to score this job against the candidate.

Rubric:
{SCORING_RUBRIC}

Candidate profile:
{json.dumps(candidate_context, ensure_ascii=False, indent=2)}

Base resume:
{base_resume_text}

        Job metadata:
{json.dumps(
    {
        "job_id": job.id,
        "tracking_id": job.tracking_id or job.id,
        "company": job.company_name or "",
        "title": job.job_title or "",
        "posted_date": job.posted_date or "",
        "apply_url": job.apply_url or "",
        "company_url": job.company_url or "",
    },
    ensure_ascii=False,
    indent=2,
)}

Job description:
{job.job_description or ""}
"""

    def _parse_score(self, response_text: str) -> int:
        fenced_match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL | re.IGNORECASE)
        candidate_text = fenced_match.group(1) if fenced_match else response_text

        match = re.search(r"\{.*\}", candidate_text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
                raw_score = payload.get("overall", payload.get("score"))
                return self._coerce_score(raw_score)
            except json.JSONDecodeError:
                pass

        score_match = re.search(r'"(?:overall|score)"\s*:\s*(-?\d+(?:\.\d+)?)', candidate_text, re.IGNORECASE)
        if score_match:
            return self._coerce_score(score_match.group(1))

        raise ValueError("Anthropic scoring response did not contain a parseable score.")

    @staticmethod
    def _coerce_score(raw_score: Any) -> int:
        try:
            score = round(float(str(raw_score).strip()))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid score returned: {raw_score!r}") from exc
        return max(0, min(1000, score))

    @staticmethod
    def _report_step(step_reporter, name: str, status: str, message: str) -> None:
        if step_reporter is not None:
            step_reporter(name=name, status=status, message=message)

    @staticmethod
    def _is_cancelled(cancel_checker) -> bool:
        return bool(cancel_checker is not None and cancel_checker())
