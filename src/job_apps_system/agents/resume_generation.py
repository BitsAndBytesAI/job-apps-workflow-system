from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apps_system.config.models import GoogleManagedFolderConfig, GoogleManagedResourcesConfig
from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.google.drive import ensure_drive_folder, validate_drive_resource
from job_apps_system.schemas.resumes import ManagedGoogleFolderSchema, ResumeAgentSummary
from job_apps_system.services.setup_config import (
    load_google_managed_resources,
    load_setup_config,
    save_google_managed_resources,
)


StepReporter = Callable[[str, str, str], None]

ROOT_FOLDER_NAME = "Job Apps Workflow System"
MANAGED_FOLDER_NAMES: dict[str, str] = {
    "resume_docs_folder": "Resume Docs",
    "resume_pdfs_folder": "Resume PDFs",
    "interview_recordings_folder": "Interview Recordings",
    "interview_transcripts_folder": "Interview Transcripts",
}


class ResumeGenerationAgent:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id

    def run(self, step_reporter: StepReporter | None = None) -> ResumeAgentSummary:
        managed_resources = self.ensure_managed_folders(step_reporter=step_reporter)
        pending_jobs = self._count_pending_jobs()

        if step_reporter:
            step_reporter(
                "Inspect pending resume jobs",
                "completed",
                f"Found {pending_jobs} job(s) without a resume URL.",
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

        return ResumeAgentSummary(
            ok=True,
            message=f"Managed Google Drive folders are ready. Pending jobs without resumes: {pending_jobs}.",
            pending_jobs=pending_jobs,
            created_folders=created_folders,
        )

    def ensure_managed_folders(self, step_reporter: StepReporter | None = None) -> GoogleManagedResourcesConfig:
        if not self._config.google.resources.base_resume_doc:
            raise ValueError("Base Resume Doc must be configured before running the Resume Agent.")

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
            resume_docs_folder=self._ensure_folder(existing.resume_docs_folder, MANAGED_FOLDER_NAMES["resume_docs_folder"], parent_id=root.resource_id),
            resume_pdfs_folder=self._ensure_folder(existing.resume_pdfs_folder, MANAGED_FOLDER_NAMES["resume_pdfs_folder"], parent_id=root.resource_id),
            interview_recordings_folder=self._ensure_folder(existing.interview_recordings_folder, MANAGED_FOLDER_NAMES["interview_recordings_folder"], parent_id=root.resource_id),
            interview_transcripts_folder=self._ensure_folder(existing.interview_transcripts_folder, MANAGED_FOLDER_NAMES["interview_transcripts_folder"], parent_id=root.resource_id),
        )

        save_google_managed_resources(self._session, managed)
        self._session.flush()

        if step_reporter:
            step_reporter(
                "Ensure managed Google Drive folders",
                "completed",
                "Managed Google Drive folders are ready.",
            )

        return managed

    def _count_pending_jobs(self) -> int:
        rows = self._session.scalars(select(Job.resume_url).where(Job.project_id == self._project_id)).all()
        return sum(1 for value in rows if not (value or "").strip())

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
