"""Deployment-wide system settings."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class SystemSetting(Base):
    """The singleton deployment setting shared by all browser sessions."""

    __tablename__ = "system_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_system_settings_singleton"),
        CheckConstraint(
            "locale IN ('zh-CN', 'en')",
            name="ck_system_settings_locale",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    locale: Mapped[str] = mapped_column(
        String(10), nullable=False, default="zh-CN", server_default=text("'zh-CN'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
