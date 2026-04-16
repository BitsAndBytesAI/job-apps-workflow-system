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
    "You are a helpful, intelligent job matching agent. Your job is to compare a job description "
    "to a set of skills and keywords to help determine a score of how well the job description matches "
    "the skills and keywords."
)
SCORING_MODEL = "claude-sonnet-4-5-20250929"
SCORING_BATCH_SIZE = 10
SCORING_BATCH_WAIT_SECONDS = 90

SCORING_SKILLS_SUMMARY = """Leadership & Management Skills

Led teams of 3-8+ software engineers across multiple organizations
Servant leadership approach with hands-on technical involvement
Cross-functional team management (frontend, backend, DevOps)
Mentorship and professional development programs (15% retention improvement)
Performance management and career growth guidance
Stakeholder relationship management and strategic planning

Technical Architecture & Development

Full-stack development expertise (React, Node.js, TypeScript, JavaScript)
Cloud-native architecture design and implementation (AWS)
Microservices architecture migration from monolithic systems
Serverless computing and containerization (Docker, Kubernetes)
API development and integration (REST, GraphQL, OAuth 2.0)
Database management (PostgreSQL, DynamoDB, Redshift)

DevOps & Infrastructure

CI/CD pipeline implementation (GitHub Actions, Jenkins, CircleCI)
Infrastructure as Code (CloudFormation)
Automated testing and deployment strategies
Performance monitoring (Prometheus, Grafana, CloudWatch, New Relic)
Feature flag management (LaunchDarkly)
Security implementation (AWS Cognito, CIAM, MFA)

Data Engineering & Analytics

Real-time streaming analytics (Apache Flink, Kinesis, MSK)
Data Lake and Data Warehouse implementation (S3, Redshift)
ETL processes and data pipeline automation (AWS Glue, EMR, Spark)
Analytics and reporting tools integration (Mixpanel, Firebase Analytics)
Search and recommendation engines (AWS OpenSearch)

Project Management & Agile

Scrum Master, Product Owner, and Agile Certified Practitioner
Sprint planning, backlog grooming, and retrospectives
Risk identification and mitigation strategies
Process optimization and workflow automation (35% task reduction)
JIRA administration and custom workflow implementation

Mobile Development

React Native application development
Cross-platform mobile architecture
Offline capabilities and data synchronization
Biometric authentication and secure storage
GPS and sensor integration"""

SCORING_RESUME_KEYWORDS = """Technical Keywords
Programming Languages & Frameworks:
React, React Native, Node.js, TypeScript, JavaScript, Java, Next.js, NestJS, Express.js, Redux, GraphQL
Cloud & DevOps:
AWS (Lambda, S3, ECS, API Gateway, Cognito, CloudFormation, Amplify), Docker, Kubernetes, Serverless, CI/CD, GitHub Actions, Jenkins
Data & Analytics:
Apache Flink, Kinesis, EMR, Spark, Redshift, S3, Data Lake, Data Warehouse, ETL, OpenSearch, Athena, Glue
Databases:
PostgreSQL, DynamoDB, RDS
Tools & Platforms:
JIRA, Figma, Storybook, LaunchDarkly, Prometheus, Grafana, Mixpanel, Firebase, Auth0, OAuth 2.0, SAML
Management Keywords
Software Engineering Manager, Engineering Manager, Technical Delivery Manager, Program Manager, Team Leadership, Cross-functional Teams, Stakeholder Management, Strategic Planning
Methodology Keywords
Scrum, Agile, SAFe (Scaled Agile Framework), Kanban, Sprint Planning, Retrospectives, TDD (Test-Driven Development), DevOps, Microservices Architecture
Certification Keywords
AWS Certified (Solutions Architect, Developer, SysOps, Security, AI Practitioner, Data Engineer), Certified Scrum Master (CSM), Certified Scrum Product Owner (CSPO), PMI Agile Certified Practitioner (ACP), Oracle Certified Java Programmer
Industry Keywords
SaaS, Cloud Migration, Digital Transformation, Scalability, Performance Optimization, Security Compliance (GDPR, CCPA), Real-time Analytics, E-commerce, Mobile Applications
Measurable Impact Keywords
25% productivity increase, 30% time-to-market reduction, 70% performance improvement, 40% bug reduction, 50% UI development time reduction, 80% deployment risk reduction"""


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
        self._report_step(step_reporter, "Score jobs", "running", f"Scoring {len(pending_jobs)} job(s) with Anthropic.")

        attempted_count = 0
        total_jobs = len(pending_jobs)
        for batch_start in range(0, total_jobs, SCORING_BATCH_SIZE):
            batch_jobs = pending_jobs[batch_start : batch_start + SCORING_BATCH_SIZE]
            batch_number = (batch_start // SCORING_BATCH_SIZE) + 1
            batch_total = (total_jobs + SCORING_BATCH_SIZE - 1) // SCORING_BATCH_SIZE

            self._report_step(
                step_reporter,
                "Score jobs",
                "running",
                f"Scoring batch {batch_number}/{batch_total} ({len(batch_jobs)} job(s)).",
            )

            for job in batch_jobs:
                attempted_count += 1
                if self._is_cancelled(cancel_checker):
                    self._report_step(
                        step_reporter,
                        "Score jobs",
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
                        "Score jobs",
                        "running",
                        f"Scored {attempted_count}/{len(pending_jobs)}: {job.company_name or 'Unknown company'} — {job.job_title or 'Untitled role'} = {score}.",
                    )
                except Exception as exc:
                    failed_count += 1
                    self._session.rollback()
                    self._report_step(
                        step_reporter,
                        "Score jobs",
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
            "Score jobs",
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
        return f"""You need to score how well a job description matches a set of provided skills and resume keywords on a scale from 0 to 100. Some jobs may not be a good match which is why I want you to go through each job description and let me know if I would be a decent fit for that job based on my skills summary and resume keywords. The better the job description matches the provided skills summary and resume keywords the higher the score should be. I will be providing you with two lists and a job description that you will use to determine the job matching score, one list is the skills summary and the other list is the resume keywords. You will also be provided the job description. If the title contains the word salesforce or if the job description contains the word salesforce more than once then the max score that can be given for that particular job is 70 regardless of how well it matches the resume.

Below is a list of my skills summary:
{SCORING_SKILLS_SUMMARY}

---

Below is a list of my resume keywords:
{SCORING_RESUME_KEYWORDS}

---

Here is the job description:
{job.job_description or ""}

----

Respond in this exact JSON format:
{{
  "score": 0,
  "company": {json.dumps(job.company_name or "")},
  "title": {json.dumps(job.job_title or "")},
  "jobDescription": {json.dumps(job.job_description or "")},
  "applyURL": {json.dumps(job.apply_url or "")},
  "companyURL": {json.dumps(job.company_url or "")},
  "ID": {json.dumps(job.id)},
  "trackingID": {json.dumps(job.tracking_id or job.id)}
}}
Only return JSON."""

    def _parse_score(self, response_text: str) -> int:
        fenced_match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL | re.IGNORECASE)
        candidate_text = fenced_match.group(1) if fenced_match else response_text

        match = re.search(r"\{.*\}", candidate_text, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
                raw_score = payload.get("score")
                return self._coerce_score(raw_score)
            except json.JSONDecodeError:
                pass

        score_match = re.search(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', candidate_text, re.IGNORECASE)
        if score_match:
            return self._coerce_score(score_match.group(1))

        raise ValueError("Anthropic scoring response did not contain a parseable score.")

    @staticmethod
    def _coerce_score(raw_score: Any) -> int:
        try:
            score = int(float(str(raw_score).strip()))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid score returned: {raw_score!r}") from exc
        return max(0, min(100, score))

    @staticmethod
    def _report_step(step_reporter, name: str, status: str, message: str) -> None:
        if step_reporter is not None:
            step_reporter(name=name, status=status, message=message)

    @staticmethod
    def _is_cancelled(cancel_checker) -> bool:
        return bool(cancel_checker is not None and cancel_checker())
