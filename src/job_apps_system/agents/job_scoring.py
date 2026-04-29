from __future__ import annotations

import hashlib
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

SCORING_DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "Required hard-skill coverage",
        "weight": 18,
        "anchors": {"0": "<30%", "5": "60–70%", "10": "≥95%, with depth"},
    },
    {
        "name": "Skill recency",
        "weight": 8,
        "anchors": {"0": "last used >5y ago", "5": "2–3y ago", "10": "currently using"},
    },
    {
        "name": "Skill depth",
        "weight": 10,
        "anchors": {"0": "listed only", "5": "one project", "10": "shipped, owned, multi-year"},
    },
    {
        "name": "Years of experience vs. JD ask",
        "weight": 8,
        "anchors": {"0": "<50% of asked, or >2× (overqualified)", "5": "within 70–90%", "10": "within ±15% of asked"},
    },
    {
        "name": "Seniority/scope alignment",
        "weight": 10,
        "anchors": {"0": "two levels off", "5": "one level off", "10": "exact match"},
    },
    {
        "name": "Domain/industry match",
        "weight": 8,
        "anchors": {"0": "unrelated domain", "5": "adjacent", "10": "direct prior experience"},
    },
    {
        "name": "Stack specificity",
        "weight": 8,
        "anchors": {"0": "none of the named stack", "5": "partial overlap, transferable", "10": "full named-stack overlap"},
    },
    {
        "name": "Role-shape match",
        "weight": 6,
        "anchors": {"0": "inverted shape", "5": "partial", "10": "exact shape"},
    },
    {
        "name": "Trajectory fit",
        "weight": 6,
        "anchors": {"0": "regression", "5": "lateral", "10": "clear next step"},
    },
    {
        "name": "Company-stage match",
        "weight": 4,
        "anchors": {"0": "only-ever-opposite stage", "5": "mixed exposure", "10": "direct match"},
    },
    {
        "name": "Mission/product affinity signal",
        "weight": 6,
        "anchors": {"0": "zero signal", "5": "one indirect signal", "10": "explicit prior work in this exact space"},
    },
    {
        "name": "Logistics fit",
        "weight": 4,
        "anchors": {"0": "hard blocker", "5": "partial friction", "10": "clean match"},
    },
    {
        "name": "Differentiator presence",
        "weight": 4,
        "anchors": {"0": "none", "5": "one preferred plus", "10": "multiple rare pluses"},
    },
)

SCORING_MODIFIER_LIMITS: dict[str, tuple[int, int]] = {
    "Hard disqualifier present": (-999, 0),
    "Title/level inflation risk": (-30, -15),
    "Domain-transfer evidence": (10, 25),
    "Quantified impact in JD's target area": (5, 20),
    "Stale-but-deep core skill": (-20, -10),
    "Over-specialization mismatch": (-25, -10),
    "Cultural/working-style signals": (-5, 5),
}


def _build_scoring_rubric() -> str:
    lines = [
        "Job ↔ Resume Match Rubric",
        "",
        "Score each dimension 0.0–10.0 against the explicit anchors below, multiply by the weight, and sum. Apply modifiers last for sub-point resolution. Final score = 0–1000.",
        "",
        "Core dimensions (weights sum to 100)",
    ]
    for index, dimension in enumerate(SCORING_DIMENSIONS, start=1):
        anchors = dimension["anchors"]
        lines.extend(
            [
                f"{index}. {dimension['name']} — weight {dimension['weight']}",
                f"   0: {anchors['0']}",
                f"   5: {anchors['5']}",
                f"   10: {anchors['10']}",
            ]
        )
    lines.extend(
        [
            "",
            "Modifiers (apply after subtotal)",
            "- Hard disqualifier present (missing visa, missing required clearance, explicit exclusion): set total below 200 and do not score further.",
            "- Title/level inflation risk: −15 to −30.",
            "- Domain-transfer evidence: +10 to +25.",
            "- Quantified impact in JD's target area: +5 to +20.",
            "- Stale-but-deep core skill: −10 to −20.",
            "- Over-specialization mismatch: −10 to −25.",
            "- Cultural/working-style signals: ±5.",
            "",
            "Scoring discipline",
            "1. Use one decimal on every dimension score.",
            "2. Cite exact resume evidence and exact job-description evidence for every dimension. If either citation is missing, that dimension caps at 5.0.",
            "3. If a dimension cannot be evaluated from the inputs, set score to null and provide na_reason. Do not default to 5.0. Weight will be redistributed in code.",
            "4. Calibration: 950+ means extremely strong fit, 500 is a coin flip, 200 is a polite rejection. Use the full range.",
            "5. Do not compute the final weighted total yourself beyond a best-effort 'overall'. The application will recompute it.",
            "",
            "Output contract",
            "{",
            '  "overall": 0-1000.00,',
            '  "verdict": "strong | plausible | stretch | mismatch | disqualified",',
            '  "dimensions": [',
            '    {"name":"...", "score":0.0 or null, "weight":18, "evidence_resume":"...", "evidence_jd":"...", "na_reason":"..."}',
            "  ],",
            '  "modifiers_applied": [{"name":"...", "delta":0, "reason":"..."}],',
            '  "top_strengths": ["...", "...", "..."],',
            '  "top_gaps": ["...", "...", "..."],',
            '  "single_sentence_summary": "..."',
            "}",
            "",
            "Return JSON only. Do not add prose before or after the JSON.",
        ]
    )
    return "\n".join(lines)


SCORING_RUBRIC = _build_scoring_rubric()


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
                        max_tokens=3200,
                        temperature=0,
                    )
                    score = self._score_response(response_text)
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

    def _score_response(self, response_text: str) -> int:
        payload = self._parse_scoring_payload(response_text)
        return self._compute_score(payload)

    def _parse_scoring_payload(self, response_text: str) -> dict[str, Any]:
        fenced_match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL | re.IGNORECASE)
        candidate_text = fenced_match.group(1) if fenced_match else response_text

        match = re.search(r"\{.*\}", candidate_text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                pass

        raise ValueError("Anthropic scoring response did not contain valid JSON.")

    def _compute_score(self, payload: dict[str, Any]) -> int:
        dimensions_by_name = {
            self._normalize_dimension_name(str(item.get("name") or "")): item
            for item in payload.get("dimensions", [])
            if isinstance(item, dict)
        }

        scored_dimensions: list[dict[str, Any]] = []
        weighted_dimensions: list[dict[str, Any]] = []
        missing_weight = 0
        for rubric_dimension in SCORING_DIMENSIONS:
            name = rubric_dimension["name"]
            weight = int(rubric_dimension["weight"])
            provided = dimensions_by_name.get(self._normalize_dimension_name(name), {})
            score = self._coerce_dimension_score(provided.get("score"))
            evidence_resume = str(provided.get("evidence_resume") or "").strip()
            evidence_jd = str(provided.get("evidence_jd") or "").strip()
            if score is not None and (not evidence_resume or not evidence_jd):
                score = min(score, 5.0)
            dimension_payload = {
                "name": name,
                "weight": weight,
                "score": score,
                "evidence_resume": evidence_resume,
                "evidence_jd": evidence_jd,
                "na_reason": str(provided.get("na_reason") or "").strip(),
            }
            scored_dimensions.append(dimension_payload)
            if score is None:
                missing_weight += weight
            else:
                weighted_dimensions.append(dimension_payload)

        if not weighted_dimensions:
            raise ValueError("Scoring response did not contain any usable dimension scores.")

        redistribution_multiplier = 100 / (100 - missing_weight) if missing_weight and missing_weight < 100 else 1.0
        subtotal = 0.0
        for dimension in weighted_dimensions:
            effective_weight = dimension["weight"] * redistribution_multiplier
            subtotal += dimension["score"] * effective_weight

        modifier_total = 0
        hard_disqualifier = False
        for modifier in payload.get("modifiers_applied", []):
            if not isinstance(modifier, dict):
                continue
            modifier_name = str(modifier.get("name") or "").strip()
            delta = self._coerce_modifier_delta(modifier_name, modifier.get("delta"))
            if delta is None:
                continue
            modifier_total += delta
            if self._normalize_modifier_name(modifier_name) == self._normalize_modifier_name("Hard disqualifier present"):
                hard_disqualifier = True

        base_total = subtotal + modifier_total
        tie_breaker = self._tie_breaker(payload)
        total = base_total + tie_breaker

        verdict = str(payload.get("verdict") or "").strip().lower()
        if hard_disqualifier or verdict == "disqualified":
            total = min(total, 199.0 + tie_breaker)

        return self._coerce_score(total)

    @staticmethod
    def _normalize_dimension_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    @staticmethod
    def _normalize_modifier_name(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    @staticmethod
    def _coerce_dimension_score(raw_score: Any) -> float | None:
        if raw_score is None:
            return None
        if isinstance(raw_score, str) and raw_score.strip().lower() in {"n/a", "na", "null", ""}:
            return None
        try:
            score = round(float(str(raw_score).strip()), 1)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(10.0, score))

    @staticmethod
    def _coerce_modifier_delta(modifier_name: str, raw_delta: Any) -> int | None:
        normalized_name = JobScoringAgent._normalize_modifier_name(modifier_name)
        for known_name, (lower, upper) in SCORING_MODIFIER_LIMITS.items():
            if normalized_name == JobScoringAgent._normalize_modifier_name(known_name):
                try:
                    delta = int(round(float(str(raw_delta).strip())))
                except (TypeError, ValueError):
                    return None
                if known_name == "Hard disqualifier present":
                    return delta
                if lower <= upper:
                    return max(lower, min(upper, delta))
                return max(upper, min(lower, delta))
        return None

    @staticmethod
    def _tie_breaker(payload: dict[str, Any]) -> float:
        evidence_quotes: list[str] = []
        for dimension in payload.get("dimensions", []):
            if not isinstance(dimension, dict):
                continue
            resume_quote = str(dimension.get("evidence_resume") or "").strip()
            jd_quote = str(dimension.get("evidence_jd") or "").strip()
            if resume_quote:
                evidence_quotes.append(resume_quote)
            if jd_quote:
                evidence_quotes.append(jd_quote)
            if len(evidence_quotes) >= 6:
                break
        joined = "||".join(evidence_quotes[:6])
        if not joined:
            return 0.0
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
        bucket = int(digest[:2], 16) % 100
        return bucket / 100

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
