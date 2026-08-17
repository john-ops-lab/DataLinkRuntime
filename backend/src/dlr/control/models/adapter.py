"""Adapter and immutable Revision persistence models (M5.4.1)."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class Adapter(Base):
    """Logical management object of an Adapter."""

    __tablename__ = "adapters"
    __table_args__ = (
        CheckConstraint(
            "language IN ('python', 'javascript', 'java')",
            name="ck_adapters_language",
        ),
        CheckConstraint(
            "adapter_type IN ('task', 'webhook')",
            name="ck_adapters_adapter_type",
        ),
        CheckConstraint(
            "run_mode IN ('manual', 'schedule')",
            name="ck_adapters_run_mode",
        ),
        # M5.5.9: names must be unique among active Adapters only; soft-deleted
        # names are reusable. This partial index is the database final defense
        # for concurrent create/rename (service pre-checks are authoritative).
        Index(
            "uq_adapters_active_name",
            "name",
            unique=True,
            postgresql_where=text("archived_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    # Fixed when the Adapter is created; all immutable Versions inherit it.
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    # Fixed when the Adapter is created; no generic Trigger framework exists.
    adapter_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Task user model: manual by default; schedule mode reveals and governs
    # the singleton AdapterSchedule configuration.
    run_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="manual", server_default=text("'manual'")
    )
    # Circular reference with adapter_versions: these foreign keys use
    # use_alter so DDL is emitted after both tables exist.
    latest_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("adapter_versions.id", use_alter=True, name="fk_adapters_latest_version_id"),
        nullable=True,
    )
    # The deterministic Worker used by every Execution of this Adapter.
    runtime_worker_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("workers.id", ondelete="SET NULL", name="fk_adapters_runtime_worker_id"),
        nullable=True,
    )
    # Soft-delete marker; NULL means the Adapter is active.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Refreshed by the database clock on every UPDATE so all timestamps of an
    # Adapter come from a single clock source.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AdapterVersion(Base):
    """Immutable snapshot created by one explicit Save."""

    __tablename__ = "adapter_versions"
    __table_args__ = (
        UniqueConstraint("adapter_id", "seq", name="uq_adapter_versions_adapter_id_seq"),
        CheckConstraint("seq > 0", name="ck_adapter_versions_seq_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Adapter-local version number: 1, 2, 3, ... assigned under a row lock.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    # pip-style requirements text; M1 stores it verbatim without parsing.
    requirements: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    # Non-sensitive configuration; must be a JSON object. Real secrets never
    # live in the database.
    runtime_config: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
