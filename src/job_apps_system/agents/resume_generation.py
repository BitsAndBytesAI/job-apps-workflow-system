from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import re
import time

from markdown import markdown as render_markdown
from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.config.models import GoogleManagedFolderConfig, GoogleManagedResourcesConfig
from job_apps_system.config.resource_ids import normalize_google_resource_id
from job_apps_system.db.models.jobs import Job
from job_apps_system.db.models.resumes import ResumeArtifact
from job_apps_system.integrations.google.docs import GoogleDocsClient
from job_apps_system.integrations.google.drive import (
    create_google_doc_from_html,
    copy_drive_file,
    ensure_drive_folder,
    export_drive_file,
    replace_drive_file_html,
    upload_drive_file,
    validate_drive_resource,
)
from job_apps_system.integrations.llm.openai_client import OpenAIClient
from job_apps_system.schemas.resumes import (
    GeneratedResumeSchema,
    ManagedGoogleFolderSchema,
    ResumeAgentSummary,
)
from job_apps_system.services.setup_config import (
    load_google_managed_resources,
    load_setup_config,
    save_google_managed_resources,
)


StepReporter = Callable[[str, str, str], None]

ROOT_FOLDER_NAME = "Job Apps Workflow System"
PROMPT_VERSION = "resume-agent-v5"
RESUME_PROVIDER = "openai"
MANAGED_FOLDER_NAMES: dict[str, str] = {
    "resume_docs_folder": "Resume Docs",
    "resume_pdfs_folder": "Resume PDFs",
    "interview_recordings_folder": "Interview Recordings",
    "interview_transcripts_folder": "Interview Transcripts",
}
@dataclass
class PendingResumeJob:
    job_id: str
    company_name: str
    job_title: str
    job_description: str
    apply_url: str
    company_url: str


class ResumeGenerationAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._docs: GoogleDocsClient | None = None
        self._cancel_checker = None

    def run(
        self,
        *,
        limit: int | None = None,
        job_ids: list[str] | None = None,
        step_reporter: StepReporter | None = None,
        cancel_checker=None,
    ) -> ResumeAgentSummary:
        self._cancel_checker = cancel_checker
        managed_resources = self.ensure_managed_folders(step_reporter=step_reporter)
        pending_jobs = self._get_pending_jobs(limit=limit, job_ids=job_ids)
        resume_provider, resume_model = self._selected_resume_provider_and_model()

        if step_reporter:
            step_reporter(
                "Inspect pending resume jobs",
                "completed",
                f"Found {len(pending_jobs)} qualifying application job(s) without a resume URL.",
            )

        created_folders = [
            ManagedGoogleFolderSchema(
                resource_id=folder.resource_id,
                name=folder.name,
                url=folder.url,
            )
            for folder in (
                managed_resources.root_folder,
                managed_resources.resume_docs_folder,
                managed_resources.resume_pdfs_folder,
                managed_resources.interview_recordings_folder,
                managed_resources.interview_transcripts_folder,
            )
            if folder is not None
        ]

        if not pending_jobs:
            return ResumeAgentSummary(
                ok=True,
                cancelled=False,
                message="No qualifying application jobs are waiting for resume generation.",
                provider=resume_provider,
                model=resume_model,
                pending_jobs=0,
                attempted_count=0,
                generated_count=0,
                failed_count=0,
                created_folders=created_folders,
                resumes=[],
            )

        base_resume_doc = self._config.google.resources.base_resume_doc
        base_resume_text = (self._config.project_resume.extracted_text or "").strip()
        if not base_resume_text and not base_resume_doc:
            raise ValueError("A base resume must be configured before running the Resume Agent.")

        generated: list[GeneratedResumeSchema] = []
        failed_count = 0

        self._report_step(step_reporter, "Load base resume", "running", "Loading the base resume content.")
        if not base_resume_text and base_resume_doc:
            base_resume_text = self._docs_client().get_document_text(base_resume_doc)
        self._report_step(step_reporter, "Load base resume", "completed", "Base resume loaded.")
        cancelled_summary = self._cancelled_summary(
            step_reporter,
            resume_provider,
            resume_model,
            len(pending_jobs),
            0,
            0,
            0,
            created_folders,
            generated,
            "Load base resume",
            "Cancellation requested. Resume agent stopped after loading the base resume.",
        )
        if cancelled_summary is not None:
            return cancelled_summary

        for index, job in enumerate(pending_jobs, start=1):
            progress_prefix = f"({index}/{len(pending_jobs)})"
            cancelled_summary = self._cancelled_summary(
                step_reporter,
                resume_provider,
                resume_model,
                len(pending_jobs),
                index - 1,
                len(generated),
                failed_count,
                created_folders,
                generated,
                "Generate tailored resume content",
                f"Cancellation requested. Resume agent stopped after processing {index - 1} of {len(pending_jobs)} job(s).",
            )
            if cancelled_summary is not None:
                return cancelled_summary
            try:
                self._report_step(
                    step_reporter,
                    "Generate tailored resume content",
                    "running",
                    f"{progress_prefix} Generating content for {job.company_name} — {job.job_title}.",
                )
                content = self._generate_resume_text(base_resume_text, job)
                self._report_step(
                    step_reporter,
                    "Generate tailored resume content",
                    "completed",
                    f"{progress_prefix} Generated resume markdown for {job.company_name}.",
                )

                self._report_step(
                    step_reporter,
                    "Format resume HTML",
                    "running",
                    f"{progress_prefix} Formatting resume HTML for {job.company_name}.",
                )
                resume_html = self._format_resume_html(content)
                self._report_step(
                    step_reporter,
                    "Format resume HTML",
                    "completed",
                    f"{progress_prefix} Formatted resume HTML for {job.company_name}.",
                )

                candidate_name, _, _, _ = self._extract_resume_header(
                    self._normalize_core_skills_section(self._normalize_resume_markdown(content))
                )

                self._report_step(
                    step_reporter,
                    "Create Google Doc",
                    "running",
                    f"{progress_prefix} Creating Google Doc for {job.company_name}.",
                )
                doc_title = self._document_title_for_candidate(job, candidate_name)
                if base_resume_doc:
                    copied_doc = copy_drive_file(
                        base_resume_doc,
                        name=doc_title,
                        parent_id=managed_resources.resume_docs_folder.resource_id if managed_resources.resume_docs_folder else None,
                        session=self._session,
                    )
                    copied_doc = replace_drive_file_html(
                        copied_doc["resource_id"],
                        html=resume_html,
                        session=self._session,
                    )
                else:
                    copied_doc = create_google_doc_from_html(
                        doc_title,
                        html=resume_html,
                        parent_id=managed_resources.resume_docs_folder.resource_id if managed_resources.resume_docs_folder else None,
                        session=self._session,
                    )
                self._report_step(
                    step_reporter,
                    "Create Google Doc",
                    "completed",
                    f"{progress_prefix} Created Google Doc for {job.company_name}.",
                )

                self._report_step(
                    step_reporter,
                    "Export PDF",
                    "running",
                    f"{progress_prefix} Exporting PDF for {job.company_name}.",
                )
                time.sleep(1.0)
                pdf_bytes = export_drive_file(
                    copied_doc["resource_id"],
                    mime_type="application/pdf",
                    session=self._session,
                )
                pdf_file = upload_drive_file(
                    f"{doc_title}.pdf",
                    content=pdf_bytes,
                    mime_type="application/pdf",
                    parent_id=managed_resources.resume_pdfs_folder.resource_id if managed_resources.resume_pdfs_folder else None,
                    session=self._session,
                )
                self._report_step(
                    step_reporter,
                    "Export PDF",
                    "completed",
                    f"{progress_prefix} Exported PDF for {job.company_name}.",
                )

                self._report_step(
                    step_reporter,
                    "Update job record",
                    "running",
                    f"{progress_prefix} Writing resume URL back to the local job record.",
                )
                self._update_local_records(
                    job=job,
                    base_resume_doc=base_resume_doc,
                    copied_doc=copied_doc,
                    pdf_file=pdf_file,
                )
                self._report_step(
                    step_reporter,
                    "Update job record",
                    "completed",
                    f"{progress_prefix} Updated the local job record for {job.company_name}.",
                )

                generated.append(
                    GeneratedResumeSchema(
                        job_id=job.job_id,
                        company_name=job.company_name,
                        job_title=job.job_title,
                        tailored_doc_url=copied_doc.get("url"),
                        pdf_url=pdf_file.get("url"),
                        provider=resume_provider,
                        model=resume_model,
                    )
                )
            except Exception as exc:
                failed_count += 1
                self._report_step(
                    step_reporter,
                    "Generate tailored resume content",
                    "failed",
                    f"{progress_prefix} Failed for {job.company_name}: {exc}",
                )

        attempted_count = len(pending_jobs)
        generated_count = len(generated)
        ok = failed_count == 0
        message = (
            f"Resume agent generated {generated_count} resume(s) from {attempted_count} pending job(s)."
            if ok
            else f"Resume agent generated {generated_count} resume(s) and failed on {failed_count} job(s)."
        )

        return ResumeAgentSummary(
            ok=ok,
            cancelled=False,
            message=message,
            provider=resume_provider,
            model=resume_model,
            pending_jobs=attempted_count,
            attempted_count=attempted_count,
            generated_count=generated_count,
            failed_count=failed_count,
            created_folders=created_folders,
            resumes=generated,
        )

    def _cancelled_summary(
        self,
        step_reporter: StepReporter | None,
        provider: str,
        model: str,
        pending_jobs: int,
        attempted_count: int,
        generated_count: int,
        failed_count: int,
        created_folders: list[ManagedGoogleFolderSchema],
        resumes: list[GeneratedResumeSchema],
        step_name: str,
        step_message: str,
    ) -> ResumeAgentSummary | None:
        if not self._is_cancelled():
            return None
        self._report_step(step_reporter, step_name, "completed", step_message)
        return ResumeAgentSummary(
            ok=False,
            cancelled=True,
            message="Resume agent cancelled.",
            provider=provider,
            model=model,
            pending_jobs=pending_jobs,
            attempted_count=attempted_count,
            generated_count=generated_count,
            failed_count=failed_count,
            created_folders=created_folders,
            resumes=list(resumes),
        )

    def ensure_managed_folders(self, step_reporter: StepReporter | None = None) -> GoogleManagedResourcesConfig:
        if step_reporter:
            step_reporter(
                "Ensure managed Google Drive folders",
                "running",
                "Checking for required Google Drive folders.",
            )

        existing = load_google_managed_resources(self._session)
        root = self._ensure_folder(existing.root_folder, f"{ROOT_FOLDER_NAME} - {self._project_id}")

        managed = GoogleManagedResourcesConfig(
            root_folder=root,
            resume_docs_folder=self._ensure_folder(
                existing.resume_docs_folder,
                MANAGED_FOLDER_NAMES["resume_docs_folder"],
                parent_id=root.resource_id,
            ),
            resume_pdfs_folder=self._ensure_folder(
                existing.resume_pdfs_folder,
                MANAGED_FOLDER_NAMES["resume_pdfs_folder"],
                parent_id=root.resource_id,
            ),
            interview_recordings_folder=self._ensure_folder(
                existing.interview_recordings_folder,
                MANAGED_FOLDER_NAMES["interview_recordings_folder"],
                parent_id=root.resource_id,
            ),
            interview_transcripts_folder=self._ensure_folder(
                existing.interview_transcripts_folder,
                MANAGED_FOLDER_NAMES["interview_transcripts_folder"],
                parent_id=root.resource_id,
            ),
        )

        save_google_managed_resources(self._session, managed)
        self._session.flush()
        self._session.commit()

        if step_reporter:
            step_reporter(
                "Ensure managed Google Drive folders",
                "completed",
                "Managed Google Drive folders are ready.",
            )

        return managed

    def _get_pending_jobs(self, *, limit: int | None, job_ids: list[str] | None) -> list[PendingResumeJob]:
        selected_job_ids = {job_id.strip() for job_id in (job_ids or []) if job_id.strip()}
        query = select(Job).where(Job.project_id == self._project_id)
        query = query.where((Job.intake_decision.is_(None)) | (Job.intake_decision == "accepted"))
        query = query.where(Job.score.is_not(None), Job.score >= self._config.app.score_threshold)
        query = query.where((Job.resume_url.is_(None)) | (Job.resume_url == ""))
        if selected_job_ids:
            query = query.where(Job.id.in_(selected_job_ids))
        records = self._session.scalars(
            query.order_by(Job.created_time.asc().nullslast(), Job.id.asc())
        ).all()
        pending = [
            PendingResumeJob(
                job_id=record.id,
                company_name=record.company_name or "",
                job_title=record.job_title or "",
                job_description=record.job_description or "",
                apply_url=record.apply_url or "",
                company_url=record.company_url or "",
            )
            for record in records
            if record.job_description
        ]

        pending.sort(key=lambda item: (item.company_name.lower(), item.job_title.lower(), item.job_id))
        if limit is not None and limit > 0:
            return pending[:limit]
        return pending

    def _update_local_records(
        self,
        *,
        job: PendingResumeJob,
        base_resume_doc: str,
        copied_doc: dict,
        pdf_file: dict,
    ) -> None:
        record_id = self._record_id(job.job_id)
        job_row = self._session.get(Job, record_id)
        if job_row is not None:
            job_row.resume_url = pdf_file.get("url")

        resume_id = record_id
        resume_row = self._session.get(ResumeArtifact, resume_id)
        if resume_row is None:
            resume_row = ResumeArtifact(id=resume_id, project_id=self._project_id, job_id=job.job_id)
            self._session.add(resume_row)

        now = datetime.now(timezone.utc)
        resume_row.base_resume_doc_id = normalize_google_resource_id(base_resume_doc) if base_resume_doc else None
        resume_row.tailored_doc_id = copied_doc.get("resource_id")
        resume_row.tailored_doc_url = copied_doc.get("url")
        resume_row.pdf_drive_file_id = pdf_file.get("resource_id")
        resume_row.pdf_drive_url = pdf_file.get("url")
        resume_row.status = "generated"
        resume_row.prompt_version = PROMPT_VERSION
        resume_row.generated_at = now
        resume_row.updated_at = now
        self._session.flush()
        self._session.commit()

    def _generate_resume_text(self, base_resume_text: str, job: PendingResumeJob) -> str:
        system_prompt = (
            "You are a technical resume customization assistant and resume creator. "
            "You are an expert in tailoring resumes to fit a job description and creating applicant tracking system "
            "(ATS) compliant resumes."
        )
        user_prompt = (
            f"I am looking to apply for {self._config.app.job_role} jobs. "
            "Your task is to customize a provided resume based on a job description that I will be providing. "
            "Be sure to include as many of the skills and requirements from the job description as you can in the customized resume. "
            "Also try and keep as much of the original content from the provided resume. "
            "The goal is to ensure that the provided resume also includes a majority of the skills and keywords from the job description. "
            "Anything that is in the job description but is missing from the resume should be incorporated into the existing bullet points "
            "of the resume rather than listed separately if possible. "
            "You should keep the candidate name and contact information intact, and you should change the role heading of the newly created resume "
            "to match the job title which I will provide.\n\n"
            "Return ONLY markdown using this structure:\n"
            "1. First line: `# Full Name`\n"
            "2. Second line: single contact line with location | phone | email | LinkedIn\n"
            "3. Third line: role title line\n"
            "4. Use only `##` for section headings such as Professional Summary, Core Skills, Professional Certifications, Work Experience, Education.\n"
            "5. Under Work Experience, use `### Role Title | Dates` followed by `Company, Location` on the next line, then bullet points.\n"
            "6. Under Core Skills, do NOT use bullets. Instead write 4-6 category rows in this exact style: `**Category:** item, item, item`.\n"
            "7. Use bullet lists for accomplishments only, especially under Work Experience.\n"
            "8. Do not use tables.\n"
            "9. Keep whitespace compact. Do not add unnecessary blank lines.\n"
            "10. Do not include Job ID in the output.\n"
            "11. Do not use code fences.\n\n"
            "----\n"
            f"Here is the job description:\n{job.job_description}\n"
            "----\n"
            f"Here is my resume:\n{base_resume_text}\n"
            "----\n"
            f"Here is the job title:\n{job.job_title}\n\n"
            "Respond with the customized, updated resume only. "
            "This resume will be added to a Google Doc so you need to write it in Markdown format (ATX) so that it can be copied and pasted. "
            "Do not use any backticks (```)"
        )
        content = self._generate_with_selected_resume_provider(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.7,
        )
        if not content:
            raise ValueError("Resume generation returned empty content.")
        return content

    def _format_resume_html(self, resume_markdown: str) -> str:
        normalized_markdown = self._normalize_resume_markdown(resume_markdown)
        normalized_markdown = self._normalize_core_skills_section(normalized_markdown)
        name, contact_line, title_line, body_markdown = self._extract_resume_header(normalized_markdown)
        rendered_body = render_markdown(body_markdown, extensions=["extra", "sane_lists"])
        body_html = self._style_resume_body_html(rendered_body)

        header_parts: list[str] = []
        if name:
            header_parts.append(
                f'<div style="text-align:center;font-size:24pt;font-weight:700;line-height:1.05;margin:0 0 4pt 0;">{escape(name)}</div>'
            )
        if contact_line:
            header_parts.append(
                f'<div style="text-align:center;font-size:9.5pt;line-height:1.2;margin:0 0 8pt 0;">{escape(contact_line)}</div>'
            )
        if title_line:
            header_parts.append(
                f'<div style="font-size:11.5pt;font-weight:700;line-height:1.2;margin:0 0 6pt 0;padding-bottom:2pt;">{escape(title_line)}</div>'
            )

        header_html = "".join(header_parts)
        return (
            "<html><body style=\"font-family:'Times New Roman', Times, serif;color:#000;margin:0.18in 0.45in 0.35in 0.45in;\">"
            f"{header_html}{body_html}"
            "</body></html>"
        )

    def _generate_with_selected_resume_provider(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
    ) -> str:
        _, model = self._selected_resume_provider_and_model()
        return OpenAIClient(session=self._session).generate_text(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )

    def _selected_resume_provider_and_model(self) -> tuple[str, str]:
        return RESUME_PROVIDER, self._config.models.openai_model

    def _docs_client(self) -> GoogleDocsClient:
        if self._docs is None:
            self._docs = GoogleDocsClient(session=self._session)
        return self._docs

    def _is_cancelled(self) -> bool:
        return bool(self._cancel_checker is not None and self._cancel_checker())

    def _normalize_resume_markdown(self, markdown_text: str) -> str:
        cleaned = markdown_text.replace("```markdown", "").replace("```html", "").replace("```", "")
        cleaned = re.sub(r"(?im)^\s*job id:\s*.*$", "", cleaned)
        cleaned = re.sub(r"\r\n?", "\n", cleaned)

        normalized_lines: list[str] = []
        known_sections = {
            "professional summary",
            "summary",
            "core skills",
            "skills",
            "professional certifications",
            "certifications",
            "work experience",
            "experience",
            "education",
            "projects",
        }

        for raw_line in cleaned.split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                normalized_lines.append("")
                continue
            if self._looks_like_horizontal_rule_line(stripped):
                continue
            bulletless = re.sub(r"^[-*]\s*", "", stripped).strip()
            lowered_bulletless = bulletless.lower().rstrip(":")
            if lowered_bulletless in known_sections:
                normalized_lines.append(f"## {bulletless.rstrip(':').title()}")
                continue
            if re.match(r"^[-*]\s+", stripped):
                normalized_lines.append(stripped)
                continue
            if stripped.startswith("#"):
                normalized_lines.append(re.sub(r"^#+\s*", lambda m: "#" * len(m.group(0).strip()) + " ", stripped))
                continue
            lowered = stripped.lower().rstrip(":")
            if lowered in known_sections:
                normalized_lines.append(f"## {stripped.rstrip(':').title()}")
                continue
            if stripped.isupper() and 2 < len(stripped) <= 40 and not any(ch.isdigit() for ch in stripped):
                normalized_lines.append(f"## {stripped.title()}")
                continue
            normalized_lines.append(raw_line.rstrip())

        spaced_lines: list[str] = []
        for line in normalized_lines:
            stripped = line.strip()
            if (
                re.match(r"^[-*]\s+", stripped)
                and spaced_lines
                and spaced_lines[-1].strip()
                and not re.match(r"^[-*]\s+|^#+\s+", spaced_lines[-1].strip())
            ):
                spaced_lines.append("")
            spaced_lines.append(line)

        normalized = "\n".join(spaced_lines)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return normalized

    def _normalize_core_skills_section(self, markdown_text: str) -> str:
        lines = markdown_text.split("\n")
        result: list[str] = []
        index = 0

        while index < len(lines):
            line = lines[index]
            if line.strip().lower() not in {"## core skills", "## skills"}:
                result.append(line)
                index += 1
                continue

            result.append(line)
            index += 1
            section_lines: list[str] = []
            while index < len(lines) and not re.match(r"^\s*##\s+", lines[index]):
                section_lines.append(lines[index])
                index += 1

            bullet_items = [
                re.sub(r"^[-*]\s*", "", item.strip())
                for item in section_lines
                if re.match(r"^[-*]\s+", item.strip())
            ]
            has_bucket_rows = any(
                ":" in item and not re.match(r"^[-*]\s+", item.strip())
                for item in section_lines
                if item.strip()
            )

            if bullet_items and not has_bucket_rows:
                bucketed = self._bucket_core_skills(bullet_items)
                if result and result[-1].strip():
                    result.append("")
                for bucket_name, values in bucketed:
                    result.append(f"**{bucket_name}:** {', '.join(values)}")
                    result.append("")
                while result and not result[-1].strip():
                    result.pop()
                result.append("")
            else:
                previous_bucket_row = False
                for section_line in section_lines:
                    stripped = section_line.strip()
                    is_bucket_row = bool(re.match(r"^\*\*[^*]+:\*\*", stripped))
                    if is_bucket_row and result and result[-1].strip():
                        result.append("")
                    result.append(section_line)
                    if is_bucket_row:
                        result.append("")
                    previous_bucket_row = is_bucket_row
                if previous_bucket_row:
                    while result and not result[-1].strip():
                        result.pop()
                    result.append("")

        normalized = "\n".join(result)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
        return normalized

    def _bucket_core_skills(self, skills: list[str]) -> list[tuple[str, list[str]]]:
        buckets: dict[str, list[str]] = {
            "Leadership": [],
            "Architecture & Cloud": [],
            "Delivery & Product": [],
            "Languages & Tools": [],
            "Additional": [],
        }
        seen: set[str] = set()

        for skill in skills:
            cleaned = re.sub(r"\s+", " ", skill).strip(" -•\t")
            if not cleaned:
                continue
            normalized_key = cleaned.lower()
            if normalized_key in seen:
                continue
            seen.add(normalized_key)

            lowered = normalized_key
            if any(token in lowered for token in ["lead", "mentor", "coach", "hire", "team", "stakeholder", "performance", "collaboration"]):
                buckets["Leadership"].append(cleaned)
            elif any(token in lowered for token in ["aws", "azure", "gcp", "cloud", "microservice", "architecture", "distributed", "kubernetes", "terraform", "devops", "cicd", "ci/cd"]):
                buckets["Architecture & Cloud"].append(cleaned)
            elif any(token in lowered for token in ["product", "roadmap", "delivery", "quality", "agile", "scrum", "program", "scope", "process", "planning", "execution"]):
                buckets["Delivery & Product"].append(cleaned)
            elif any(token in lowered for token in ["java", "javascript", "typescript", "python", "react", "node", "sql", "graphql", "api", "git", "docker", "lambda", "spark", "figma", "vscode"]):
                buckets["Languages & Tools"].append(cleaned)
            else:
                buckets["Additional"].append(cleaned)

        ordered = [(name, values) for name, values in buckets.items() if values]
        if not ordered:
            return [("Core Skills", skills[:12])]
        return ordered

    def _extract_resume_header(self, markdown_text: str) -> tuple[str, str, str, str]:
        lines = markdown_text.split("\n")
        name = ""
        contact_line = ""
        title_line = ""
        body_start = 0

        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# "):
                name = stripped[2:].strip()
            else:
                name = stripped.lstrip("#").strip()
            body_start = index + 1
            break

        header_lines: list[str] = []
        for index in range(body_start, len(lines)):
            stripped = lines[index].strip()
            if not stripped:
                if header_lines:
                    body_start = index + 1
                    break
                continue
            if self._looks_like_contact_line(stripped):
                header_lines.append(stripped)
                body_start = index + 1
                continue
            body_start = index
            break

        if header_lines:
            contact_line = " | ".join(part.strip(" |") for part in header_lines if part.strip(" |"))

        body_lines = lines[body_start:]
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)

        if body_lines:
            first_line = body_lines[0].strip()
            if (
                first_line
                and not first_line.startswith("#")
                and not first_line.startswith(("-", "*"))
                and len(first_line.split()) <= 12
                and not re.search(r"[@|]", first_line)
            ):
                title_line = first_line
                body_lines = body_lines[1:]

        body_markdown = "\n".join(body_lines).strip()
        return name, contact_line, title_line, body_markdown

    @staticmethod
    def _looks_like_contact_line(value: str) -> bool:
        lowered = value.lower()
        return (
            "@" in value
            or "linkedin" in lowered
            or "|" in value
            or bool(re.search(r"\(\d{3}\)|\d{3}[-.\s]\d{3}[-.\s]\d{4}", value))
        )

    @staticmethod
    def _looks_like_horizontal_rule_line(value: str) -> bool:
        candidate = re.sub(r"^[-*•]\s+", "", value.strip())
        compacted = re.sub(r"\s+", "", candidate)
        return bool(re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,}|—{3,}|─{3,})", compacted))

    def _style_resume_body_html(self, html: str) -> str:
        styled = html.strip()
        styled = re.sub(r"(?is)<p>\s*job id:.*?</p>", "", styled)
        styled = re.sub(r"(?is)<li>\s*<hr\s*/?>\s*</li>", "", styled)
        styled = re.sub(r"(?is)<hr\s*/?>", "", styled)
        styled = re.sub(
            r"(?is)<h2>(.*?)</h2>",
            lambda match: (
                '<div style="font-size:11pt;font-weight:700;text-transform:uppercase;'
                'letter-spacing:0.02em;margin:10pt 0 5pt 0;padding-bottom:2pt;">'
                f"{match.group(1).strip()}"
                "</div>"
            ),
            styled,
        )
        styled = re.sub(
            r"(?is)<h3>(.*?)</h3>",
            self._render_role_heading,
            styled,
        )
        styled = re.sub(
            r"(?is)<p>",
            '<p style="margin:0 0 4pt 0;font-size:10.5pt;line-height:1.18;">',
            styled,
        )
        styled = re.sub(
            r"(?is)<ul>",
            '<ul style="margin:0 0 6pt 0;padding-left:18pt;">',
            styled,
        )
        styled = re.sub(
            r"(?is)<ol>",
            '<ol style="margin:0 0 6pt 0;padding-left:18pt;">',
            styled,
        )
        styled = re.sub(
            r"(?is)<li>",
            '<li style="margin:0 0 2pt 0;padding-left:2pt;">',
            styled,
        )
        styled = re.sub(r"\n{3,}", "\n\n", styled)
        return styled

    def _render_role_heading(self, match: re.Match[str]) -> str:
        heading_html = match.group(1).strip()
        heading_text = re.sub(r"(?is)<[^>]+>", "", heading_html).strip()
        if "|" in heading_text:
            left, right = [part.strip() for part in heading_text.split("|", 1)]
            return (
                '<div style="font-size:10.5pt;font-weight:700;margin:8pt 0 2pt 0;overflow:hidden;">'
                f'<span style="float:left;max-width:72%;">{escape(left)}</span>'
                f'<span style="float:right;white-space:nowrap;">{escape(right)}</span>'
                "</div>"
            )
        return f'<div style="font-size:10.5pt;font-weight:700;margin:8pt 0 2pt 0;">{heading_html}</div>'


    def _document_title(self, job: PendingResumeJob) -> str:
        return self._document_title_for_candidate(job, self._config.app.job_role)

    def _document_title_for_candidate(self, job: PendingResumeJob, candidate_name: str) -> str:
        candidate = self._safe_filename(candidate_name or "Candidate")
        company = self._safe_filename(job.company_name or "Company")
        title = self._safe_filename(job.job_title or self._config.app.job_role)
        return f"{candidate}-{company}-{title}-Resume"

    def _safe_filename(self, value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip()
        collapsed = re.sub(r"\s+", "-", cleaned)
        compacted = re.sub(r"-{2,}", "-", collapsed).strip("-")
        return compacted[:80] or "Document"

    def _ensure_folder(
        self,
        existing: GoogleManagedFolderConfig | None,
        name: str,
        *,
        parent_id: str | None = None,
    ) -> GoogleManagedFolderConfig:
        folder = None
        if existing is not None and existing.resource_id:
            existing_resource = validate_drive_resource(existing.resource_id, session=self._session)
            if existing_resource["ok"]:
                folder = existing_resource

        if folder is None:
            folder = ensure_drive_folder(name=name, parent_id=parent_id, session=self._session)

        return GoogleManagedFolderConfig(
            resource_id=folder["resource_id"],
            name=folder["name"] or name,
            url=folder.get("url"),
        )

    def _record_id(self, job_id: str) -> str:
        return f"{self._project_id}:{job_id}"

    @staticmethod
    def _report_step(step_reporter, name: str, status: str, message: str) -> None:
        if step_reporter is not None:
            step_reporter(name=name, status=status, message=message)
