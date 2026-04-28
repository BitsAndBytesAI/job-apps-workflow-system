from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from job_apps_system.db.base import Base


class UnansweredApplicationQuestion(Base):
    __tablename__ = "unanswered_application_questions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "job_id",
            "question_text",
            name="uq_unanswered_application_questions_project_job_question",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    company_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    ats_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
