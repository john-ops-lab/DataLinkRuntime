"""Administrative placement authorization and immutable route references."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class WorkerEnvironmentClass(Base):
    __tablename__ = "worker_environment_classes"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    architecture: Mapped[str] = mapped_column(String(16), nullable=False)
    toolchains: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    builtin_materials: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class WorkerPool(Base):
    __tablename__ = "worker_pools"
    __table_args__ = (
        CheckConstraint(
            "version >= current_revision AND current_revision > 0",
            name="ck_worker_pool_versions",
        ),
        ForeignKeyConstraint(
            ["id", "current_revision"],
            ["worker_pool_revisions.pool_id", "worker_pool_revisions.revision"],
            name="fk_worker_pool_current_revision",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("1"))
    current_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )


class WorkerPoolRevision(Base):
    __tablename__ = "worker_pool_revisions"
    __table_args__ = (CheckConstraint("revision > 0", name="ck_worker_pool_revision_positive"),)

    pool_id: Mapped[int] = mapped_column(
        ForeignKey("worker_pools.id", ondelete="RESTRICT"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    selector: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    environment_class: Mapped[str] = mapped_column(
        ForeignKey("worker_environment_classes.name", ondelete="RESTRICT"), nullable=False
    )
    placement_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkerPoolMember(Base):
    __tablename__ = "worker_pool_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_pool_member_identity",
            ondelete="RESTRICT",
        ),
    )

    pool_id: Mapped[int] = mapped_column(
        ForeignKey("worker_pools.id", ondelete="RESTRICT"), primary_key=True
    )
    worker_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_identity_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    labels: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    placement_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    credential_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    material_scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class WorkerPoolAudit(Base):
    __tablename__ = "worker_pool_audit"
    __table_args__ = (Index("ix_worker_pool_audit_pool_id", "pool_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("worker_pools.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def pool_reference(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["route_pool_id", "route_pool_revision"],
        ["worker_pool_revisions.pool_id", "worker_pool_revisions.revision"],
        name=name,
        ondelete="RESTRICT",
        match="FULL",
    )


class RouteSnapshotColumns:
    """Immutable placement requirements stored with the durable responsibility."""

    route_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    route_pool_id: Mapped[int | None] = mapped_column(
        BigInteger, Computed("(route_snapshot #>> '{route,pool_id}')::bigint", persisted=True)
    )
    route_pool_revision: Mapped[int | None] = mapped_column(
        BigInteger, Computed("(route_snapshot #>> '{route,pool_revision}')::bigint", persisted=True)
    )
