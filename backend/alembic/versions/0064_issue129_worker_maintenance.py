"""Store Worker maintenance versions and idempotent audit facts."""

import sqlalchemy as sa
from alembic import op

revision = "0064_issue129_worker_maintenance"
down_revision = "0063_issue129_legacy_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers",
        sa.Column(
            "maintenance_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
    )
    op.create_check_constraint(
        "ck_workers_maintenance_version", "workers", "maintenance_version >= 1"
    )
    op.create_table(
        "worker_maintenance_operations",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("expected_version", sa.BigInteger(), nullable=False),
        sa.Column("result_version", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        sa.Column("reason", sa.String(256)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.pg_catalog.clock_timestamp(),
        ),
        sa.UniqueConstraint("worker_id", "result_version", name="uq_worker_maintenance_result"),
        sa.CheckConstraint(
            "request_id <> '00000000-0000-0000-0000-000000000000'::uuid",
            name="ck_worker_maintenance_request_nonzero",
        ),
        sa.CheckConstraint(
            "expected_version >= 1 AND result_version = expected_version + 1",
            name="ck_worker_maintenance_operation_version",
        ),
        sa.CheckConstraint(
            "(action IN ('drain', 'activate') AND reason IS NULL) OR "
            "(action = 'force_stop' AND reason IS NOT NULL AND "
            "length(reason) BETWEEN 1 AND 256 AND length(btrim(reason)) > 0)",
            name="ck_worker_maintenance_operation_action",
        ),
        sa.CheckConstraint(
            "(actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0)",
            name="ck_worker_maintenance_operation_actor",
        ),
    )
    op.execute("""
        CREATE FUNCTION dlr_worker_maintenance_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'Worker maintenance audit is immutable' USING ERRCODE = '23514';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_maintenance_immutable
        BEFORE UPDATE OR DELETE ON worker_maintenance_operations
        FOR EACH ROW EXECUTE FUNCTION dlr_worker_maintenance_immutable()
    """)
    op.execute(
        "ALTER TABLE worker_maintenance_operations ENABLE ALWAYS TRIGGER "
        "dlr_worker_maintenance_immutable"
    )


def downgrade() -> None:
    # Exclude new guarded state before checking it and removing its storage.
    op.execute("LOCK TABLE workers, worker_maintenance_operations IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_maintenance_operations) THEN
                RAISE EXCEPTION 'worker maintenance audit prevents downgrade';
            END IF;
        END $$
    """)
    op.drop_table("worker_maintenance_operations")
    op.execute("DROP FUNCTION dlr_worker_maintenance_immutable()")
    op.drop_constraint("ck_workers_maintenance_version", "workers", type_="check")
    op.drop_column("workers", "maintenance_version")
