"""Persisted configuration for the productized KnowledgeSource integrations."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class KnowledgeSourceSetting(Base):
    """The singleton administrator configuration for the first KnowledgeSource.

    The table intentionally has no seed row.  An absent row is the compatibility
    signal that the runtime must keep using the deployment environment fallback.
    """

    __tablename__ = "knowledge_source_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_knowledge_source_settings_singleton"),
        CheckConstraint(
            "source_id IN ('ima')",
            name="ck_knowledge_source_settings_source_id",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    source_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ima", server_default=text("'ima'")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    credential_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
