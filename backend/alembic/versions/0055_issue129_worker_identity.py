"""Add stable Worker identity and instance state without replacing old rows."""

import sqlalchemy as sa
from alembic import op

revision = "0055_issue129_worker_identity"
down_revision = "0054_issue222_receipt_ttl_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL evaluates this volatile default for each existing row, preserving
    # the old primary key and every referencing business record.
    op.add_column(
        "workers",
        sa.Column(
            "identity_id", sa.Uuid(), nullable=False, server_default=sa.text("gen_random_uuid()")
        ),
    )
    op.add_column("workers", sa.Column("identity_enrolled_at", sa.DateTime(timezone=True)))
    op.add_column("workers", sa.Column("instance_id", sa.Uuid()))
    op.add_column(
        "workers",
        sa.Column("instance_generation", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("workers", sa.Column("instance_lease_until", sa.DateTime(timezone=True)))
    op.add_column(
        "workers",
        sa.Column("scheduling_state", sa.String(16), nullable=False, server_default="active"),
    )
    op.add_column(
        "workers",
        sa.Column("capacity_state", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.create_unique_constraint("uq_workers_identity_id", "workers", ["identity_id"])
    op.create_unique_constraint("uq_workers_id_identity", "workers", ["id", "identity_id"])
    # A Worker identity is a durable binding, not a mutable capability report.
    # Check again after all BEFORE triggers so another trigger cannot replace
    # the identity after this function's first check.
    op.execute("""
        CREATE FUNCTION dlr_worker_identity_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.id IS DISTINCT FROM OLD.id OR
             NEW.identity_id IS DISTINCT FROM OLD.identity_id THEN
            RAISE EXCEPTION 'Worker identity is immutable' USING ERRCODE = '23514';
          END IF;
          IF OLD.identity_enrolled_at IS NOT NULL AND
             NEW.identity_enrolled_at IS DISTINCT FROM OLD.identity_enrolled_at THEN
            RAISE EXCEPTION 'Worker enrollment is immutable' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_identity_immutable
        BEFORE UPDATE ON workers
        FOR EACH ROW EXECUTE FUNCTION dlr_worker_identity_immutable()
    """)
    op.execute("""
        CREATE TRIGGER dlr_worker_identity_immutable_after
        AFTER UPDATE ON workers
        FOR EACH ROW EXECUTE FUNCTION dlr_worker_identity_immutable()
    """)
    op.execute("ALTER TABLE workers ENABLE ALWAYS TRIGGER dlr_worker_identity_immutable")
    op.execute("ALTER TABLE workers ENABLE ALWAYS TRIGGER dlr_worker_identity_immutable_after")
    op.create_check_constraint(
        "ck_workers_scheduling_state",
        "workers",
        "scheduling_state IN ('active', 'draining', 'disabled')",
    )
    op.create_check_constraint(
        "ck_workers_capacity_state",
        "workers",
        "capacity_state IN ('unknown', 'reconciling', 'ready', 'quarantined')",
    )
    op.create_check_constraint(
        "ck_workers_instance_generation", "workers", "instance_generation >= 0"
    )
    op.create_check_constraint(
        "ck_workers_instance_lease",
        "workers",
        "(instance_id IS NULL AND instance_lease_until IS NULL) OR "
        "(instance_id IS NOT NULL AND instance_lease_until IS NOT NULL "
        "AND instance_generation > 0)",
    )
    op.create_table(
        "worker_credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "worker_id",
            sa.BigInteger(),
            sa.ForeignKey("workers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("secret_digest", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('enrollment', 'runtime')", name="ck_worker_credentials_kind"),
        sa.CheckConstraint("octet_length(secret_digest) = 32", name="ck_worker_credentials_digest"),
        sa.CheckConstraint(
            "kind <> 'enrollment' OR expires_at IS NOT NULL",
            name="ck_worker_credentials_enrollment_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR kind = 'enrollment'", name="ck_worker_credentials_consumed_kind"
        ),
        sa.UniqueConstraint("secret_digest", name="uq_worker_credentials_secret_digest"),
        sa.UniqueConstraint("worker_id", "id", name="uq_worker_credentials_worker_record"),
    )
    op.create_index("ix_worker_credentials_worker_id", "worker_credentials", ["worker_id"])
    op.create_table(
        "worker_credential_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("credential_record_id", sa.BigInteger(), nullable=False),
        sa.Column("related_credential_record_id", sa.BigInteger()),
        sa.Column("event_kind", sa.String(16), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.pg_catalog.clock_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "credential_record_id"],
            ["worker_credentials.worker_id", "worker_credentials.id"],
            name="fk_worker_credential_audit_record",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["worker_id", "related_credential_record_id"],
            ["worker_credentials.worker_id", "worker_credentials.id"],
            name="fk_worker_credential_audit_related",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "event_kind IN ('enrollment', 'exchange', 'rotate', 'revoke', 'recover')",
            name="ck_worker_credential_audit_event",
        ),
        sa.CheckConstraint(
            "(actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0) OR "
            "(actor_kind = 'worker' AND actor_id IS NULL)",
            name="ck_worker_credential_audit_actor",
        ),
        sa.CheckConstraint(
            "(event_kind = 'exchange' AND actor_kind = 'worker') OR "
            "(event_kind <> 'exchange' AND actor_kind <> 'worker')",
            name="ck_worker_credential_audit_exchange_actor",
        ),
        sa.CheckConstraint(
            "(event_kind IN ('exchange', 'rotate') AND related_credential_record_id IS NOT NULL "
            "AND related_credential_record_id <> credential_record_id) OR "
            "(event_kind IN ('enrollment', 'revoke', 'recover') "
            "AND related_credential_record_id IS NULL)",
            name="ck_worker_credential_audit_related_shape",
        ),
    )
    op.create_index(
        "ix_worker_credential_audit_worker",
        "worker_credential_audit",
        ["worker_id", "id"],
    )


def downgrade() -> None:
    # Exclude new guarded state before checking it and removing its storage.
    op.execute(
        "LOCK TABLE workers, worker_credentials, worker_credential_audit IN ACCESS EXCLUSIVE MODE"
    )
    # Identity state cannot be reconstructed by the old binary. Refuse to
    # silently discard credentials, generations or administrator intent.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM worker_credential_audit) OR
             EXISTS (SELECT 1 FROM worker_credentials) OR EXISTS (
            SELECT 1 FROM workers WHERE identity_enrolled_at IS NOT NULL
              OR instance_id IS NOT NULL OR instance_generation <> 0
              OR instance_lease_until IS NOT NULL OR scheduling_state <> 'active'
              OR capacity_state <> 'unknown'
          ) THEN
            RAISE EXCEPTION 'worker identity state prevents downgrade';
          END IF;
        END $$
        """
    )
    op.drop_index("ix_worker_credential_audit_worker", table_name="worker_credential_audit")
    op.drop_table("worker_credential_audit")
    op.drop_index("ix_worker_credentials_worker_id", table_name="worker_credentials")
    op.drop_table("worker_credentials")
    op.execute("DROP TRIGGER dlr_worker_identity_immutable_after ON workers")
    op.execute("DROP TRIGGER dlr_worker_identity_immutable ON workers")
    op.execute("DROP FUNCTION dlr_worker_identity_immutable()")
    for name in (
        "ck_workers_instance_lease",
        "ck_workers_instance_generation",
        "ck_workers_capacity_state",
        "ck_workers_scheduling_state",
    ):
        op.drop_constraint(name, "workers", type_="check")
    op.drop_constraint("uq_workers_identity_id", "workers", type_="unique")
    op.drop_constraint("uq_workers_id_identity", "workers", type_="unique")
    for name in (
        "capacity_state",
        "scheduling_state",
        "instance_lease_until",
        "instance_generation",
        "instance_id",
        "identity_enrolled_at",
        "identity_id",
    ):
        op.drop_column("workers", name)
