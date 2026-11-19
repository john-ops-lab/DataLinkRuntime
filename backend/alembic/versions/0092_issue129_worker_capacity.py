"""Persist verified Worker capacity and pre-dispatch reservations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0092_issue129_worker_capacity"
down_revision = "0091_issue129_pool_routing"
branch_labels = None
depends_on = None

DIMENSIONS = ("cpu_millis", "memory_bytes", "pids", "tmp_bytes", "slots")


def _worker_binding(prefix: str) -> tuple:
    return (
        sa.ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name=f"fk_{prefix}_identity",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name=f"fk_{prefix}_instance",
        ),
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_execution_attempts_id_execution", "execution_attempts", ["id", "execution_id"]
    )
    op.create_table(
        "worker_capacities",
        sa.Column("worker_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("worker_identity_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("capacity_version", sa.BigInteger(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("envelope_fingerprint", sa.String(64), nullable=False),
        sa.Column("verified_environment", postgresql.JSONB(), nullable=False),
        sa.Column("nofile", sa.BigInteger(), nullable=False),
        sa.Column("storage_ready", sa.Boolean(), nullable=False),
        sa.Column("last_assignment_at", sa.DateTime(timezone=True)),
        *(sa.Column(name, sa.BigInteger(), nullable=False) for name in DIMENSIONS),
        *_worker_binding("worker_capacities"),
        sa.CheckConstraint(
            "capacity_version >= 1 AND nofile > 0 AND sampled_at <= received_at AND "
            + " AND ".join(f"{name} >= 0" for name in DIMENSIONS),
            name="ck_worker_capacities_values",
        ),
    )
    op.create_table(
        "worker_resource_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "execution_id",
            sa.BigInteger(),
            sa.ForeignKey("executions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dispatch_generation", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_identity_id", sa.Uuid(), nullable=False),
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column("capacity_version", sa.BigInteger(), nullable=False),
        sa.Column("envelope_fingerprint", sa.String(64), nullable=False),
        sa.Column("cpu_accounting_version", sa.BigInteger()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("held_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_id", sa.BigInteger()),
        sa.Column("fencing_token", sa.BigInteger()),
        *(sa.Column(name, sa.BigInteger(), nullable=False) for name in DIMENSIONS),
        *_worker_binding("worker_reservations"),
        sa.UniqueConstraint(
            "execution_id", "dispatch_generation", name="uq_worker_reservations_generation"
        ),
        sa.UniqueConstraint(
            "id", "execution_id", "dispatch_generation", name="uq_worker_reservations_binding"
        ),
        sa.UniqueConstraint("attempt_id", name="uq_worker_reservations_attempt"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "execution_id"],
            ["execution_attempts.id", "execution_attempts.execution_id"],
            name="fk_worker_reservations_attempt_execution",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.ForeignKeyConstraint(
            [
                "attempt_id",
                "worker_id",
                "worker_identity_id",
                "instance_id",
                "instance_generation",
                "fencing_token",
            ],
            [
                "execution_attempts.id",
                "execution_attempts.worker_id",
                "execution_attempts.worker_identity_id",
                "execution_attempts.worker_instance_id",
                "execution_attempts.worker_instance_generation",
                "execution_attempts.fencing_token",
            ],
            name="fk_worker_reservations_attempt",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "status IN ('held', 'claimed', 'reclaiming', 'released') AND "
            "((status = 'released') = (released_at IS NOT NULL)) AND "
            "((attempt_id IS NULL) = (fencing_token IS NULL)) AND "
            "(status <> 'held' OR attempt_id IS NULL) AND "
            "(status NOT IN ('claimed', 'reclaiming') OR attempt_id IS NOT NULL)",
            name="ck_worker_reservations_state",
        ),
        sa.CheckConstraint(
            "capacity_version > 0 AND dispatch_generation > 0 AND slots = 1 AND "
            "cpu_millis >= 0 AND memory_bytes > 0 AND pids > 0 AND tmp_bytes > 0",
            name="ck_worker_reservations_values",
        ),
    )
    op.create_index(
        "ix_worker_reservations_occupied",
        "worker_resource_reservations",
        ["worker_id"],
        postgresql_where=sa.text("status IN ('held', 'claimed', 'reclaiming')"),
    )
    op.add_column(
        "executions",
        sa.Column(
            "requires_worker_reservation", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column("executions", sa.Column("placement_after_worker_id", sa.BigInteger()))
    op.execute("""
        CREATE FUNCTION dlr_keep_worker_reservation_requirement() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.requires_worker_reservation IS DISTINCT FROM OLD.requires_worker_reservation THEN
                RAISE EXCEPTION 'Immutable Worker reservation requirement' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER dlr_keep_worker_reservation_requirement
          BEFORE UPDATE OF requires_worker_reservation ON executions
          FOR EACH ROW EXECUTE FUNCTION dlr_keep_worker_reservation_requirement()
    """)
    op.add_column("execution_outbox", sa.Column("reservation_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_execution_outbox_reservation",
        "execution_outbox",
        "worker_resource_reservations",
        ["reservation_id", "execution_id", "dispatch_generation"],
        ["id", "execution_id", "dispatch_generation"],
    )
    op.alter_column("execution_outbox", "routing_key", nullable=True)
    op.alter_column("execution_outbox", "payload_json", nullable=True)
    op.drop_constraint("ck_execution_outbox_status", "execution_outbox", type_="check")
    op.create_check_constraint(
        "ck_execution_outbox_status",
        "execution_outbox",
        "status IN ('awaiting_target', 'pending', 'published', 'superseded')",
    )
    op.create_check_constraint(
        "ck_execution_outbox_wire_shape",
        "execution_outbox",
        "(routing_key IS NULL) = (payload_json IS NULL) AND "
        "(status <> 'awaiting_target' OR (routing_key IS NULL AND reservation_id IS NULL)) "
        "AND (status NOT IN ('pending', 'published') OR routing_key IS NOT NULL) AND "
        "(reservation_id IS NULL OR routing_key IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_capacities)
               OR EXISTS (SELECT 1 FROM worker_resource_reservations)
               OR EXISTS (SELECT 1 FROM executions WHERE requires_worker_reservation)
               OR EXISTS (SELECT 1 FROM execution_outbox
                          WHERE status IN ('awaiting_target', 'superseded'))
               OR EXISTS (SELECT 1 FROM control_cluster_authority
                          WHERE schema_revision='0092_issue129_worker_capacity') THEN
                RAISE EXCEPTION 'Worker capacity downgrade would lose durable state';
            END IF;
        END $$;
        DROP TRIGGER dlr_keep_worker_reservation_requirement ON executions;
        DROP FUNCTION dlr_keep_worker_reservation_requirement();
    """)
    op.drop_constraint("ck_execution_outbox_wire_shape", "execution_outbox", type_="check")
    op.drop_constraint("ck_execution_outbox_status", "execution_outbox", type_="check")
    op.create_check_constraint(
        "ck_execution_outbox_status",
        "execution_outbox",
        "status IN ('pending', 'published')",
    )
    op.alter_column("execution_outbox", "routing_key", nullable=False)
    op.alter_column("execution_outbox", "payload_json", nullable=False)
    op.drop_constraint("fk_execution_outbox_reservation", "execution_outbox", type_="foreignkey")
    op.drop_column("execution_outbox", "reservation_id")
    op.drop_column("executions", "requires_worker_reservation")
    op.drop_column("executions", "placement_after_worker_id")
    op.drop_table("worker_resource_reservations")
    op.drop_table("worker_capacities")
    op.drop_constraint("uq_execution_attempts_id_execution", "execution_attempts", type_="unique")
