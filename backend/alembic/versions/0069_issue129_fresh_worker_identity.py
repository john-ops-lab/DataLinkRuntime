"""Reserve new Worker identities without enabling legacy name takeover."""

import sqlalchemy as sa
from alembic import op

revision = "0069_issue129_fresh_identity"
down_revision = "0066_issue129_v4_recovery_grace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The old enrollment timestamp remains authoritative for already enrolled
    # identities; this flag distinguishes reserved fresh identities pre-exchange.
    op.add_column(
        "workers",
        sa.Column("identity_lifecycle", sa.String(16), nullable=False, server_default="legacy"),
    )
    op.create_check_constraint(
        "ck_workers_identity_lifecycle", "workers", "identity_lifecycle IN ('legacy', 'reserved')"
    )
    op.create_check_constraint(
        "ck_workers_reserved_protocol",
        "workers",
        "identity_lifecycle <> 'reserved' OR protocol_version = 4",
    )
    # A reserved identity must never be returned to the shared-token path by
    # an ordinary update. Check before and after other row triggers.
    op.execute("""
        CREATE FUNCTION dlr_worker_reservation_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.identity_lifecycle = 'reserved' AND
             NEW.identity_lifecycle IS DISTINCT FROM OLD.identity_lifecycle THEN
            RAISE EXCEPTION 'Worker reservation is immutable' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_reservation_immutable BEFORE UPDATE ON workers
        FOR EACH ROW EXECUTE FUNCTION dlr_worker_reservation_immutable()
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_reservation_immutable_after AFTER UPDATE ON workers
        FOR EACH ROW EXECUTE FUNCTION dlr_worker_reservation_immutable()
    """)
    op.execute("ALTER TABLE workers ENABLE ALWAYS TRIGGER dlr_worker_reservation_immutable")
    op.execute("ALTER TABLE workers ENABLE ALWAYS TRIGGER dlr_worker_reservation_immutable_after")
    op.create_table(
        "worker_identity_reservations",
        sa.Column("request_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "(actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0)",
            name="ck_worker_identity_reservation_actor",
        ),
    )


def downgrade() -> None:
    # Match reservation creation: Worker first, then its audit record. Lock
    # before checking so a concurrent commit cannot disappear during DDL.
    op.execute("LOCK TABLE workers, worker_identity_reservations IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM worker_identity_reservations) OR
             EXISTS (SELECT 1 FROM workers WHERE identity_lifecycle <> 'legacy') THEN
            RAISE EXCEPTION 'reserved Worker identities prevent downgrade';
          END IF;
        END $$
        """
    )
    op.drop_table("worker_identity_reservations")
    op.execute("DROP TRIGGER dlr_worker_reservation_immutable_after ON workers")
    op.execute("DROP TRIGGER dlr_worker_reservation_immutable ON workers")
    op.execute("DROP FUNCTION dlr_worker_reservation_immutable()")
    op.drop_constraint("ck_workers_reserved_protocol", "workers", type_="check")
    op.drop_constraint("ck_workers_identity_lifecycle", "workers", type_="check")
    op.drop_column("workers", "identity_lifecycle")
