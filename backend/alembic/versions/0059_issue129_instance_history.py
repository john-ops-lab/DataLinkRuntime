"""Retain every fenced Worker instance ID to prevent generation replay."""

import sqlalchemy as sa
from alembic import op

revision = "0059_issue129_instance_history"
down_revision = "0058_issue129_protocol_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_instance_history",
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("instance_id", sa.Uuid(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("generation > 0", name="ck_worker_instance_history_generation"),
        sa.UniqueConstraint(
            "worker_id", "generation", name="uq_worker_instance_history_generation"
        ),
        sa.UniqueConstraint(
            "worker_id", "instance_id", "generation", name="uq_worker_instance_history_binding"
        ),
    )
    # Import facts we can prove from the current instance and v4 Attempt
    # snapshots. Earlier registrations without an Attempt cannot be inferred.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (
              SELECT 1 FROM execution_attempts a JOIN workers w ON w.id = a.worker_id
              WHERE a.worker_instance_id IS NOT NULL
                AND (a.worker_identity_id IS DISTINCT FROM w.identity_id
                     OR a.worker_instance_generation > w.instance_generation)
          ) THEN
              RAISE EXCEPTION 'worker instance snapshot conflicts with current identity';
          END IF;
        END $$
    """)
    # Retired IDs are fencing evidence. Routine application writes must not
    # be able to alter or delete them and make an old process eligible again.
    # A database owner can still alter schema/trigger state; that is outside
    # this application-level guarantee.
    op.execute("""
        CREATE FUNCTION reject_worker_instance_history_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'worker instance history is immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER worker_instance_history_immutable
        BEFORE UPDATE OR DELETE ON worker_instance_history
        FOR EACH ROW EXECUTE FUNCTION reject_worker_instance_history_mutation()
    """)
    op.execute("""
        INSERT INTO worker_instance_history (worker_id, instance_id, generation)
        SELECT id, instance_id, instance_generation FROM workers WHERE instance_id IS NOT NULL
        UNION
        SELECT worker_id, worker_instance_id, worker_instance_generation
        FROM execution_attempts WHERE worker_instance_id IS NOT NULL
    """)
    # Refuse to silently manufacture unknown retired IDs. All generations
    # must be represented before the new registry can fence old processes.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (
              SELECT 1 FROM workers w
              LEFT JOIN worker_instance_history h ON h.worker_id = w.id
              GROUP BY w.id, w.instance_generation
              HAVING COUNT(h.instance_id) <> w.instance_generation
                  OR COALESCE(MAX(h.generation), 0) <> w.instance_generation
          ) THEN
              RAISE EXCEPTION 'worker instance history incomplete; reconcile before upgrade';
          END IF;
        END $$
    """)


def downgrade() -> None:
    # Exclude new guarded state before checking it and removing its storage.
    op.execute("LOCK TABLE worker_instance_history IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM worker_instance_history) THEN
              RAISE EXCEPTION 'worker instance history prevents downgrade';
          END IF;
        END $$
    """)
    op.drop_table("worker_instance_history")
    op.execute("DROP FUNCTION reject_worker_instance_history_mutation()")
