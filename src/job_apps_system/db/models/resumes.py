from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from job_apps_system.db.base import Base


class ResumeArtifact(Base):
    __tablename__ = "resumes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    project_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str] = mapped_column(Text)
    base_resume_doc_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tailored_doc_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    tailored_doc_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_drive_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_drive_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
