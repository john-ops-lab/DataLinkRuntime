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
        # M5.5.11: the authoritative single-run execution timeout in seconds.
        # Every Execution of this Adapter (manual, schedule, webhook) is
        # killed and marked ``timeout`` after this duration. No "unlimited"
        # option exists: 1 second .. 24 hours only.
        CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 86400",
            name="ck_adapters_timeout_seconds",
        ),
        CheckConstraint(
            "(template_scenario_slug IS NULL AND template_version IS NULL) "
            "OR (template_scenario_slug IS NOT NULL AND template_version IS NOT NULL)",
            name="ck_adapters_template_provenance_pair",
        ),
        # M5.5.9 compatibility: legacy archived rows do not block names. New
        # Adapter deletion is permanent; this partial index remains the final
        # defense for any pre-Wave-C archived rows and concurrent create/rename.
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
    # M5.5.11: Adapter-level authoritative single-run execution timeout in
    # seconds. Defaults to 5 minutes; 1..86400 (24h) enforced by the check
    # constraint above. Task manual and Task schedule runs share it, and the
    # Worker receives it in every task payload.
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default=text("300")
    )
    # Account users own the Adapters they create. Historical Adapters and
    # Adapters created through the deployment superadmin/admin entry remain
    # system-owned (NULL) and are only visible to ordinary users after an
    # explicit ACL grant.
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
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
    # Read-only provenance for an Adapter instantiated from one immutable
    # Gallery Recipe. Ordinary create/clone rows keep both values NULL; there
    # is intentionally no FK from user data to the static catalog.
    template_scenario_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    template_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Legacy soft-delete marker; new Wave C deletes remove the row entirely.
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


class AdapterPermission(Base):
    """One explicit read/edit grant for an account user on an Adapter."""

    __tablename__ = "adapter_permissions"
    __table_args__ = (
        CheckConstraint(
            "permission IN ('read', 'edit')",
            name="ck_adapter_permissions_permission",
        ),
        UniqueConstraint("adapter_id", "user_id", name="uq_adapter_permissions_adapter_user"),
    )

    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission: Mapped[str] = mapped_column(String(8), nullable=False)
