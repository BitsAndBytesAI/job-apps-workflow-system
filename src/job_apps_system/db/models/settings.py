from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from job_apps_system.db.base import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
