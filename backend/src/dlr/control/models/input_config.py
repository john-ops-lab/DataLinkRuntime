"""Adapter-level input configuration persistence model."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class AdapterInputSourceType(StrEnum):
    """Closed set of input sources stored by the Control plane."""

    NONE = "none"
    JSON = "json"
    MANAGED_FILES = "managed_files"
    REMOTE_FILES = "remote_files"


class AdapterInputRetentionMode(StrEnum):
    """Retention choices for the managed-file source."""

    SYSTEM_DEFAULT = "system_default"
    CUSTOM = "custom"
    MANUAL_DELETE = "manual_delete"


# Short aliases keep the domain vocabulary convenient for callers while the
# longer names make the model ownership explicit in imports.
InputSourceType = AdapterInputSourceType
InputRetentionMode = AdapterInputRetentionMode


class AdapterInputConfig(Base):
    """The one current input selection owned by a Task Adapter.

    Artifact bindings are introduced by the later Managed Input wave. A0
    stores the source/revision contract and accepts an empty ``managed_files``
    selection without pretending that an artifact exists.
    """

    __tablename__ = "adapter_input_configs"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('none', 'json', 'managed_files', 'remote_files')",
            name="ck_adapter_input_configs_source_type",
        ),
        CheckConstraint(
            "retention_mode IN ('system_default', 'custom', 'manual_delete')",
            name="ck_adapter_input_configs_retention_mode",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_adapter_input_configs_revision_positive",
        ),
        CheckConstraint(
            "(source_type = 'json' AND json_value IS NOT NULL) "
            "OR ((source_type IN ('none', 'managed_files', 'remote_files')) "
            "AND json_value IS NULL)",
            name="ck_adapter_input_configs_source_fields",
        ),
        CheckConstraint(
            "(retention_mode = 'custom' AND retention_seconds > 0) "
            "OR (retention_mode IN ('system_default', 'manual_delete') "
            "AND retention_seconds IS NULL)",
            name="ck_adapter_input_configs_retention_fields",
        ),
        Index("ix_adapter_input_configs_source_type", "source_type"),
        Index("ix_adapter_input_configs_revision", "revision"),
    )

    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=InputSourceType.NONE, server_default=text("'none'")
    )
    # JSONB stores any JSON top-level value, including JSON null. SQL NULL is
    # reserved for source types that do not carry a JSON value.
    json_value: Mapped[object | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    retention_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=InputRetentionMode.SYSTEM_DEFAULT,
        server_default=text("'system_default'"),
    )
    retention_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
