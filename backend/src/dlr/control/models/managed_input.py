"""Managed Input policy and lifecycle persistence models.

Wave B0 only establishes the public database contract.  Upload, binding,
retention, and deletion behavior are implemented by later waves; keeping the
tables here nevertheless makes their ownership and concurrency invariants
explicit for migrations and catalog checks.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class ManagedInputReservationStatus(StrEnum):
    """Terminal and active states for one upload capacity reservation."""

    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ManagedInputArtifactStatus(StrEnum):
    """Artifact lifecycle states shared by upload and GC waves."""

    UPLOADING = "UPLOADING"
    STAGED = "STAGED"
    READY = "READY"
    PENDING_DELETE = "PENDING_DELETE"
    DELETING = "DELETING"
    DELETED = "DELETED"
    DELETE_FAILED = "DELETE_FAILED"


class ManagedInputDeletionJobStatus(StrEnum):
    """Status of a deletion job after Adapter metadata is removed."""

    PENDING = "pending"
    DELETING = "deleting"
    COMPLETED = "completed"
    FAILED = "failed"


class ManagedInputSettings(Base):
    """The database-backed singleton policy for Managed Input."""

    __tablename__ = "managed_input_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_managed_input_settings_singleton"),
        CheckConstraint(
            "default_retention_seconds BETWEEN 3600 AND 2592000",
            name="ck_managed_input_settings_default_retention_seconds",
        ),
        CheckConstraint(
            "max_file_bytes BETWEEN 1048576 AND 2147483648",
            name="ck_managed_input_settings_max_file_bytes",
        ),
        CheckConstraint(
            "platform_quota_bytes BETWEEN 1048576 AND 10995116277760",
            name="ck_managed_input_settings_platform_quota_bytes",
        ),
        CheckConstraint(
            "adapter_quota_bytes BETWEEN 1048576 AND 10995116277760",
            name="ck_managed_input_settings_adapter_quota_bytes",
        ),
        CheckConstraint(
            "adapter_quota_bytes <= platform_quota_bytes",
            name="ck_managed_input_settings_adapter_quota_le_platform",
        ),
        CheckConstraint(
            "max_custom_retention_seconds BETWEEN 3600 AND 31536000",
            name="ck_managed_input_settings_max_custom_retention_seconds",
        ),
        CheckConstraint(
            "max_custom_retention_seconds >= default_retention_seconds",
            name="ck_managed_input_settings_custom_retention_ge_default",
        ),
        CheckConstraint(
            "min_free_space_bytes BETWEEN 67108864 AND 1099511627776",
            name="ck_managed_input_settings_min_free_space_bytes",
        ),
        CheckConstraint(
            "staged_ttl_seconds BETWEEN 300 AND 86400",
            name="ck_managed_input_settings_staged_ttl_seconds",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    default_retention_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_file_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    platform_quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adapter_quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    allow_manual_delete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_custom_retention_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_free_space_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    staged_ttl_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ManagedInputCapacity(Base):
    """The single serialized platform capacity account."""

    __tablename__ = "managed_input_capacity"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_managed_input_capacity_singleton"),
        CheckConstraint("actual_bytes >= 0", name="ck_managed_input_capacity_actual_bytes"),
        CheckConstraint("reserved_bytes >= 0", name="ck_managed_input_capacity_reserved_bytes"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    actual_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    reserved_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ManagedInputUploadReservation(Base):
    """One atomic upload reservation charged to an Adapter and the platform."""

    __tablename__ = "managed_input_upload_reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'CANCELLED', 'EXPIRED')",
            name="ck_managed_input_upload_reservations_status",
        ),
        CheckConstraint(
            "reserved_bytes >= 0",
            name="ck_managed_input_upload_reservations_reserved_bytes",
        ),
        UniqueConstraint("upload_session_id", name="uq_managed_input_upload_reservations_session"),
        UniqueConstraint(
            "id", "adapter_id", name="uq_managed_input_upload_reservations_id_adapter"
        ),
        Index(
            "ix_managed_input_upload_reservations_status_expires_at",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    upload_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ManagedInputReservationStatus.ACTIVE
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ManagedInputArtifact(Base):
    """Metadata for one Adapter-owned object in the future ArtifactStore."""

    __tablename__ = "managed_input_artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('UPLOADING', 'STAGED', 'READY', 'PENDING_DELETE', 'DELETING', "
            "'DELETED', 'DELETE_FAILED')",
            name="ck_managed_input_artifacts_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_managed_input_artifacts_size_bytes"),
        CheckConstraint(
            "retention_mode IN ('system_default', 'custom', 'manual_delete')",
            name="ck_managed_input_artifacts_retention_mode",
        ),
        CheckConstraint(
            "delete_attempts >= 0",
            name="ck_managed_input_artifacts_delete_attempts",
        ),
        UniqueConstraint("storage_key", name="uq_managed_input_artifacts_storage_key"),
        UniqueConstraint(
            "upload_reservation_id",
            name="uq_managed_input_artifacts_upload_reservation",
        ),
        UniqueConstraint("id", "adapter_id", name="uq_managed_input_artifacts_id_adapter"),
        ForeignKeyConstraint(
            ["upload_reservation_id", "adapter_id"],
            [
                "managed_input_upload_reservations.id",
                "managed_input_upload_reservations.adapter_id",
            ],
            ondelete="RESTRICT",
            name="fk_managed_input_artifacts_upload_reservation_adapter",
        ),
        Index("ix_managed_input_artifacts_adapter_id", "adapter_id"),
        Index(
            "ix_managed_input_artifacts_status_expires_at",
            "status",
            "expires_at",
        ),
        Index(
            "ix_managed_input_artifacts_status_delete_lease_until",
            "status",
            "delete_lease_until",
        ),
        Index("ix_managed_input_artifacts_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    upload_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    upload_reservation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(256), nullable=False)
    content_type: Mapped[str] = mapped_column(String(256), nullable=False)
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ManagedInputArtifactStatus.UPLOADING
    )
    retention_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="system_default",
        server_default=text("'system_default'"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    delete_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delete_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AdapterInputArtifactBinding(Base):
    """The current, ordered Artifact selection of one Adapter input config."""

    __tablename__ = "adapter_input_artifact_bindings"
    __table_args__ = (
        CheckConstraint(
            "input_config_revision > 0",
            name="ck_adapter_input_artifact_bindings_revision_positive",
        ),
        CheckConstraint(
            "ordinal BETWEEN 0 AND 7", name="ck_adapter_input_artifact_bindings_ordinal"
        ),
        UniqueConstraint(
            "adapter_id", "ordinal", name="uq_adapter_input_artifact_bindings_adapter_ordinal"
        ),
        ForeignKeyConstraint(
            ["adapter_id"],
            ["adapter_input_configs.adapter_id"],
            ondelete="CASCADE",
            name="fk_adapter_input_artifact_bindings_input_config",
        ),
        ForeignKeyConstraint(
            ["artifact_id", "adapter_id"],
            ["managed_input_artifacts.id", "managed_input_artifacts.adapter_id"],
            ondelete="RESTRICT",
            name="fk_adapter_input_artifact_bindings_artifact_adapter",
        ),
        Index("ix_adapter_input_artifact_bindings_artifact_id", "artifact_id"),
        Index(
            "ix_adapter_input_artifact_bindings_adapter_revision",
            "adapter_id",
            "input_config_revision",
        ),
    )

    adapter_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    input_config_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ArtifactDeletionJob(Base):
    """Blob deletion responsibility detached from deleted Adapter metadata."""

    __tablename__ = "artifact_deletion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'deleting', 'completed', 'failed')",
            name="ck_artifact_deletion_jobs_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifact_deletion_jobs_size_bytes"),
        CheckConstraint("charged_bytes >= 0", name="ck_artifact_deletion_jobs_charged_bytes"),
        CheckConstraint("attempts >= 0", name="ck_artifact_deletion_jobs_attempts"),
        UniqueConstraint("storage_key", name="uq_artifact_deletion_jobs_storage_key"),
        Index("ix_artifact_deletion_jobs_status_lease_until", "status", "delete_lease_until"),
        Index("ix_artifact_deletion_jobs_former_adapter_id", "former_adapter_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(256), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    former_adapter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    delete_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ManagedInputDeletionJobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capacity_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# Short aliases keep imports aligned with the Issue vocabulary while retaining
# explicit names for tables whose owner matters in larger service modules.
ManagedInputReservation = ManagedInputUploadReservation
ManagedInputArtifactBinding = AdapterInputArtifactBinding
ManagedInputBinding = AdapterInputArtifactBinding
ManagedInputArtifactDeletionJob = ArtifactDeletionJob
ManagedInputDeletionJob = ArtifactDeletionJob
