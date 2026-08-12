"""M3.2 adapter production lifecycle tables.

Extends ``adapters`` with the production Worker pointer, production entry
state and archive marker; extends ``executions`` with the scheduling target
Worker, the cancel-request flag and the ``production`` trigger value (the
stored value space of historical rows stays ``manual``). Adds the Secret
Store tables (``credentials`` / ``adapter_credential_bindings``) and the
platform ``package_sources`` table.

Revision ID: 0003_m3_2_production_lifecycle
Revises: 0002_m2_workers_executions
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_m3_2_production_lifecycle"
down_revision: str | None = "0002_m2_workers_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- adapters: production lifecycle fields ------------------------------
    op.add_column("adapters", sa.Column("production_worker_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "adapters",
        sa.Column(
            "production_state",
            sa.String(length=16),
            server_default=sa.text("'idle'"),
            nullable=False,
        ),
    )
    op.add_column("adapters", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_adapters_production_state",
        "adapters",
        "production_state IN ('idle', 'running', 'stopped')",
    )
    op.create_foreign_key(
        "fk_adapters_production_worker_id",
        "adapters",
        "workers",
        ["production_worker_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- executions: target worker, cancel flag, production trigger ---------
    op.add_column("executions", sa.Column("target_worker_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "executions",
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_executions_target_worker_id",
        "executions",
        "workers",
        ["target_worker_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Widen the trigger value space: historical rows keep 'manual' (the
    # documented compatibility reading is "test run"), Start creates
    # 'production' rows; future schedule/webhook triggers stay production
    # class without changing the stored classification.
    op.drop_constraint("ck_executions_trigger", "executions", type_="check")
    op.create_check_constraint(
        "ck_executions_trigger",
        "executions",
        "trigger IN ('manual', 'production')",
    )
    # One active Production Execution per Adapter, enforced by the database.
    op.execute(
        "CREATE UNIQUE INDEX uq_executions_active_production "
        "ON executions (adapter_id) "
        "WHERE trigger = 'production' AND status IN ('pending', 'running')"
    )
    op.create_index("ix_executions_target_worker_id", "executions", ["target_worker_id"])

    # --- credentials (Secret Store) ------------------------------------------
    op.create_table(
        "credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        # Fernet ciphertext; plaintext values are never persisted.
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('password', 'token', 'access_key', 'secret')",
            name="ck_credentials_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_credentials_name"),
    )

    # --- adapter credential bindings ------------------------------------------
    op.create_table(
        "adapter_credential_bindings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("adapter_id", sa.BigInteger(), nullable=False),
        # Environment key the adapter reads via context.secrets.get(env_key).
        sa.Column("env_key", sa.String(length=128), nullable=False),
        sa.Column("credential_id", sa.BigInteger(), nullable=False),
        # Which field of the credential the binding resolves to.
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["adapter_id"], ["adapters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "adapter_id", "env_key", name="uq_adapter_credential_bindings_adapter_env_key"
        ),
    )
    op.create_index(
        "ix_adapter_credential_bindings_adapter_id",
        "adapter_credential_bindings",
        ["adapter_id"],
    )

    # --- package sources -------------------------------------------------------
    op.create_table(
        "package_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("index_url", sa.Text(), nullable=False),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_package_sources_name"),
    )
    # At most one default package source.
    op.execute(
        "CREATE UNIQUE INDEX uq_package_sources_default "
        "ON package_sources (is_default) WHERE is_default"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_package_sources_default")
    op.drop_table("package_sources")
    op.drop_index(
        "ix_adapter_credential_bindings_adapter_id", table_name="adapter_credential_bindings"
    )
    op.drop_table("adapter_credential_bindings")
    op.drop_table("credentials")
    op.drop_index("ix_executions_target_worker_id", table_name="executions")
    op.execute("DROP INDEX uq_executions_active_production")
    op.drop_constraint("ck_executions_trigger", "executions", type_="check")
    op.create_check_constraint("ck_executions_trigger", "executions", "trigger IN ('manual')")
    op.drop_constraint("fk_executions_target_worker_id", "executions", type_="foreignkey")
    op.drop_column("executions", "cancel_requested")
    op.drop_column("executions", "target_worker_id")
    op.drop_constraint("fk_adapters_production_worker_id", "adapters", type_="foreignkey")
    op.drop_constraint("ck_adapters_production_state", "adapters", type_="check")
    op.drop_column("adapters", "archived_at")
    op.drop_column("adapters", "production_state")
    op.drop_column("adapters", "production_worker_id")
