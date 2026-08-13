"""Adapter and AdapterVersion persistence models.

M1 contracts kept by these models:

- An Adapter may exist with zero versions (both pointers start NULL).
- AdapterVersion rows are immutable snapshots; no Draft entity exists.
- ``latest_version_id`` / ``published_version_id`` must only be changed by the
  domain service, never by public API input.

M3.2 lifecycle fields:

- ``production_worker_id`` is deployment configuration, never part of a
  Version; changing it does not create a new Version.
- ``production_state`` models the production entry (open/closed), not the
  existence of a running subprocess.
- ``archived_at`` is the minimal archive marker; archived Adapters keep all
  Versions and Executions.

M5.1 production version locking:

- ``production_version_id`` is set by Start (to the current
  ``published_version_id``) and cleared by Stop. Publish during a running
  entry only changes ``published_version_id``; the locked production version
  stays stable until the next Stop → Start cycle.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
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
            "production_state IN ('idle', 'running', 'stopped')",
            name="ck_adapters_production_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    # Fixed when the Adapter is created; all immutable Versions inherit it.
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    # Circular reference with adapter_versions: these foreign keys use
    # use_alter so DDL is emitted after both tables exist.
    latest_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("adapter_versions.id", use_alter=True, name="fk_adapters_latest_version_id"),
        nullable=True,
    )
    published_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("adapter_versions.id", use_alter=True, name="fk_adapters_published_version_id"),
        nullable=True,
    )
    # M5.1: the version locked by the current production entry. Set by Start
    # (to published_version_id) and cleared by Stop. Publish during a running
    # entry never touches this pointer; only Stop → Start rotates it.
    production_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("adapter_versions.id", use_alter=True, name="fk_adapters_production_version_id"),
        nullable=True,
    )
    # The Worker that runs tests and production executions of this Adapter.
    production_worker_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("workers.id", ondelete="SET NULL", name="fk_adapters_production_worker_id"),
        nullable=True,
    )
    # Production entry state: idle (never started / entry closed with no
    # history), running (entry open), stopped (explicitly stopped).
    production_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle", server_default=text("'idle'")
    )
    # Archive marker; NULL means the Adapter is active.
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
