from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from job_apps_system.db.base import Base


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    interview_row_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_type: Mapped[str] = mapped_column(Text)
    recipient_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipient_name: Mapped[str] = mapped_column(Text)
    recipient_email: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
