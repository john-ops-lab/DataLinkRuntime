"""Issue #127 B0 Managed Input policy and lifecycle schema.

This migration establishes the public storage contract only.  It seeds the
database policy and serialized platform capacity account; upload, binding,
retention, GC, and deletion behavior are intentionally left to later waves.

Revision ID: 0028_issue127_b0_managed_input
Revises: 0027_issue127_a1_execution_input
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_issue127_b0_managed_input"
down_revision: str | None = "0027_issue127_a1_execution_input"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _seed_singletons() -> None:
    """Seed both singleton rows without changing an existing policy."""
    op.execute(
        sa.text(
            "INSERT INTO managed_input_settings ("
            "id, default_retention_seconds, max_file_bytes, platform_quota_bytes, "
            "adapter_quota_bytes, allow_manual_delete, max_custom_retention_seconds, "
            "min_free_space_bytes, staged_ttl_seconds"
            ") VALUES (1, 86400, 104857600, 10737418240, 1073741824, true, 2592000, "
            "1073741824, 3600) ON CONFLICT (id) DO NOTHING"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO managed_input_capacity (id, actual_bytes, reserved_bytes) "
            "VALUES (1, 0, 0) ON CONFLICT (id) DO NOTHING"
        )
    )


def upgrade() -> None:
    op.create_table(
        "managed_input_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("default_retention_seconds", sa.BigInteger(), nullable=False),
        sa.Column("max_file_bytes", sa.BigInteger(), nullable=False),
        sa.Column("platform_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("adapter_quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("allow_manual_delete", sa.Boolean(), nullable=False),
        sa.Column("max_custom_retention_seconds", sa.BigInteger(), nullable=False),
        sa.Column("min_free_space_bytes", sa.BigInteger(), nullable=False),
        sa.Column("staged_ttl_seconds", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_managed_input_settings_singleton"),
        sa.CheckConstraint(
            "default_retention_seconds BETWEEN 3600 AND 2592000",
            name="ck_managed_input_settings_default_retention_seconds",
        ),
        sa.CheckConstraint(
            "max_file_bytes BETWEEN 1048576 AND 2147483648",
            name="ck_managed_input_settings_max_file_bytes",
        ),
        sa.CheckConstraint(
            "platform_quota_bytes BETWEEN 1048576 AND 10995116277760",
            name="ck_managed_input_settings_platform_quota_bytes",
        ),
        sa.CheckConstraint(
            "adapter_quota_bytes BETWEEN 1048576 AND 10995116277760",
            name="ck_managed_input_settings_adapter_quota_bytes",
        ),
        sa.CheckConstraint(
            "adapter_quota_bytes <= platform_quota_bytes",
            name="ck_managed_input_settings_adapter_quota_le_platform",
        ),
        sa.CheckConstraint(
            "max_custom_retention_seconds BETWEEN 3600 AND 31536000",
            name="ck_managed_input_settings_max_custom_retention_seconds",
        ),
        sa.CheckConstraint(
            "max_custom_retention_seconds >= default_retention_seconds",
            name="ck_managed_input_settings_custom_retention_ge_default",
        ),
        sa.CheckConstraint(
            "min_free_space_bytes BETWEEN 67108864 AND 1099511627776",
            name="ck_managed_input_settings_min_free_space_bytes",
        ),
        sa.CheckConstraint(
            "staged_ttl_seconds BETWEEN 300 AND 86400",
            name="ck_managed_input_settings_staged_ttl_seconds",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "managed_input_capacity",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_managed_input_capacity_singleton"),
        sa.CheckConstraint("actual_bytes >= 0", name="ck_managed_input_capacity_actual_bytes"),
        sa.CheckConstraint("reserved_bytes >= 0", name="ck_managed_input_capacity_reserved_bytes"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "managed_input_upload_reservations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("upload_session_id", sa.String(length=128), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'ACTIVE'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'CANCELLED', 'EXPIRED')",
            name="ck_managed_input_upload_reservations_status",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0",
            name="ck_managed_input_upload_reservations_reserved_bytes",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"],
            ["adapters.id"],
            ondelete="RESTRICT",
            name="fk_managed_input_upload_reservations_adapter",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "upload_session_id", name="uq_managed_input_upload_reservations_session"
        ),
        sa.UniqueConstraint(
            "id", "adapter_id", name="uq_managed_input_upload_reservations_id_adapter"
        ),
    )
    op.create_index(
        "ix_managed_input_upload_reservations_adapter_id",
        "managed_input_upload_reservations",
        ["adapter_id"],
    )
    op.create_index(
        "ix_managed_input_upload_reservations_status_expires_at",
        "managed_input_upload_reservations",
        ["status", "expires_at"],
    )
    op.create_table(
        "managed_input_artifacts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("upload_session_id", sa.String(length=128), nullable=False),
        sa.Column("upload_reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("storage_key", sa.String(length=256), nullable=False),
        sa.Column("content_type", sa.String(length=256), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'UPLOADING'"),
            nullable=False,
        ),
        sa.Column(
            "retention_mode",
            sa.String(length=16),
            server_default=sa.text("'system_default'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_attempts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("delete_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('UPLOADING', 'STAGED', 'READY', 'PENDING_DELETE', 'DELETING', "
            "'DELETED', 'DELETE_FAILED')",
            name="ck_managed_input_artifacts_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_managed_input_artifacts_size_bytes"),
        sa.CheckConstraint(
            "retention_mode IN ('system_default', 'custom', 'manual_delete')",
            name="ck_managed_input_artifacts_retention_mode",
        ),
        sa.CheckConstraint(
            "delete_attempts >= 0",
            name="ck_managed_input_artifacts_delete_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"],
            ["adapters.id"],
            ondelete="RESTRICT",
            name="fk_managed_input_artifacts_adapter",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_managed_input_artifacts_created_by_user",
        ),
        sa.ForeignKeyConstraint(
            ["upload_reservation_id", "adapter_id"],
            [
                "managed_input_upload_reservations.id",
                "managed_input_upload_reservations.adapter_id",
            ],
            ondelete="RESTRICT",
            name="fk_managed_input_artifacts_upload_reservation_adapter",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_managed_input_artifacts_storage_key"),
        sa.UniqueConstraint(
            "upload_reservation_id",
            name="uq_managed_input_artifacts_upload_reservation",
        ),
        sa.UniqueConstraint("id", "adapter_id", name="uq_managed_input_artifacts_id_adapter"),
    )
    op.create_index(
        "ix_managed_input_artifacts_adapter_id", "managed_input_artifacts", ["adapter_id"]
    )
    op.create_index(
        "ix_managed_input_artifacts_created_by_user_id",
        "managed_input_artifacts",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_managed_input_artifacts_status_expires_at",
        "managed_input_artifacts",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_managed_input_artifacts_status_delete_lease_until",
        "managed_input_artifacts",
        ["status", "delete_lease_until"],
    )
    op.create_index(
        "ix_managed_input_artifacts_status_created_at",
        "managed_input_artifacts",
        ["status", "created_at"],
    )
    op.create_table(
        "adapter_input_artifact_bindings",
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_id", sa.BigInteger(), nullable=False),
        sa.Column("input_config_revision", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "input_config_revision > 0",
            name="ck_adapter_input_artifact_bindings_revision_positive",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 7",
            name="ck_adapter_input_artifact_bindings_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["adapter_id"],
            ["adapter_input_configs.adapter_id"],
            ondelete="CASCADE",
            name="fk_adapter_input_artifact_bindings_input_config",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id", "adapter_id"],
            ["managed_input_artifacts.id", "managed_input_artifacts.adapter_id"],
            ondelete="RESTRICT",
            name="fk_adapter_input_artifact_bindings_artifact_adapter",
        ),
        sa.PrimaryKeyConstraint("adapter_id", "artifact_id"),
        sa.UniqueConstraint(
            "adapter_id", "ordinal", name="uq_adapter_input_artifact_bindings_adapter_ordinal"
        ),
    )
    op.create_index(
        "ix_adapter_input_artifact_bindings_artifact_id",
        "adapter_input_artifact_bindings",
        ["artifact_id"],
    )
    op.create_index(
        "ix_adapter_input_artifact_bindings_adapter_revision",
        "adapter_input_artifact_bindings",
        ["adapter_id", "input_config_revision"],
    )
    op.create_table(
        "artifact_deletion_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("storage_key", sa.String(length=256), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("former_adapter_id", sa.BigInteger(), nullable=False),
        sa.Column("charged_bytes", sa.BigInteger(), nullable=False),
        sa.Column("delete_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("delete_attempts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("delete_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capacity_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DELETING', 'DELETED', 'DELETE_FAILED')",
            name="ck_artifact_deletion_jobs_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifact_deletion_jobs_size_bytes"),
        sa.CheckConstraint("charged_bytes >= 0", name="ck_artifact_deletion_jobs_charged_bytes"),
        sa.CheckConstraint(
            "delete_attempts >= 0", name="ck_artifact_deletion_jobs_delete_attempts"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_artifact_deletion_jobs_storage_key"),
    )
    op.create_index(
        "ix_artifact_deletion_jobs_status_lease_until",
        "artifact_deletion_jobs",
        ["status", "delete_lease_until"],
    )
    op.create_index(
        "ix_artifact_deletion_jobs_former_adapter_id",
        "artifact_deletion_jobs",
        ["former_adapter_id"],
    )
    _seed_singletons()


def downgrade() -> None:
    """Test-only cleanup path; production rollback must not downgrade schema."""
    op.drop_index(
        "ix_artifact_deletion_jobs_former_adapter_id", table_name="artifact_deletion_jobs"
    )
    op.drop_index(
        "ix_artifact_deletion_jobs_status_lease_until", table_name="artifact_deletion_jobs"
    )
    op.drop_table("artifact_deletion_jobs")
    op.drop_index(
        "ix_adapter_input_artifact_bindings_adapter_revision",
        table_name="adapter_input_artifact_bindings",
    )
    op.drop_index(
        "ix_adapter_input_artifact_bindings_artifact_id",
        table_name="adapter_input_artifact_bindings",
    )
    op.drop_table("adapter_input_artifact_bindings")
    op.drop_index(
        "ix_managed_input_artifacts_status_created_at", table_name="managed_input_artifacts"
    )
    op.drop_index(
        "ix_managed_input_artifacts_status_delete_lease_until",
        table_name="managed_input_artifacts",
    )
    op.drop_index(
        "ix_managed_input_artifacts_status_expires_at", table_name="managed_input_artifacts"
    )
    op.drop_index(
        "ix_managed_input_artifacts_created_by_user_id", table_name="managed_input_artifacts"
    )
    op.drop_index("ix_managed_input_artifacts_adapter_id", table_name="managed_input_artifacts")
    op.drop_table("managed_input_artifacts")
    op.drop_index(
        "ix_managed_input_upload_reservations_status_expires_at",
        table_name="managed_input_upload_reservations",
    )
    op.drop_index(
        "ix_managed_input_upload_reservations_adapter_id",
        table_name="managed_input_upload_reservations",
    )
    op.drop_table("managed_input_upload_reservations")
    op.drop_table("managed_input_capacity")
    op.drop_table("managed_input_settings")
