"""Offline image transition receipts; existing authority stays untouched."""

import sqlalchemy as sa
from alembic import op

revision = "0088_issue129_image_transition"
down_revision = "0087_issue129_disposition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "control_image_transitions",
        sa.Column("operation_id", sa.Uuid(), primary_key=True),
        sa.Column("cluster_id", sa.Uuid(), nullable=False),
        sa.Column("authority_epoch", sa.Uuid(), nullable=False),
        sa.Column(
            "previous_id",
            sa.Uuid(),
            sa.ForeignKey("control_image_transitions.operation_id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("target_digest", sa.String(71), nullable=False),
        sa.Column("source_schema", sa.String(32), nullable=False),
        sa.Column("target_schema", sa.String(32), nullable=False),
        sa.Column("intent_sha256", sa.String(64), nullable=False),
        sa.Column("writer_xid", sa.BigInteger(), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("writer_xid > 0", name="ck_control_image_transition_xid"),
        sa.CheckConstraint(
            "operation_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND cluster_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND authority_epoch <> '00000000-0000-0000-0000-000000000000'::uuid",
            name="ck_control_image_transition_ids",
        ),
        sa.CheckConstraint(
            "source_digest ~ '^sha256:[0-9a-f]{64}$' "
            "AND target_digest ~ '^sha256:[0-9a-f]{64}$' AND source_digest <> target_digest "
            "AND intent_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_control_image_transition_digests",
        ),
    )
    op.create_index(
        "uq_control_image_transition_root",
        "control_image_transitions",
        [sa.text("(true)")],
        unique=True,
        postgresql_where=sa.text("previous_id IS NULL"),
    )
    op.execute("REVOKE ALL ON control_image_transitions FROM PUBLIC")
    # Strip inherited default table grants as well as PUBLIC permissions.
    op.execute("""
        DO $$ DECLARE grantee_name text; BEGIN
          FOR grantee_name IN
            SELECT DISTINCT r.rolname FROM pg_catalog.pg_class c,
              LATERAL pg_catalog.aclexplode(c.relacl) a
              JOIN pg_catalog.pg_roles r ON r.oid = a.grantee
            WHERE c.oid = 'control_image_transitions'::regclass AND a.grantee <> c.relowner
          LOOP
            EXECUTE format('REVOKE ALL ON control_image_transitions FROM %I', grantee_name);
          END LOOP;
        END $$
    """)
    # Existing runtime table grants do not include this newly created table.
    # The trigger additionally rejects inherited/default grants and SET ROLE.
    op.execute("""
        CREATE FUNCTION dlr_image_transition_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'INSERT' OR current_user <> session_user OR
             current_user <> pg_catalog.pg_get_userbyid(
               (SELECT relowner FROM pg_catalog.pg_class WHERE oid = TG_RELID)) OR
             NEW.writer_xid <> pg_catalog.txid_current() THEN
            RAISE EXCEPTION 'Image transition unavailable' USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER dlr_image_transition_append_only
        BEFORE INSERT OR UPDATE OR DELETE ON control_image_transitions
        FOR EACH ROW EXECUTE FUNCTION dlr_image_transition_append_only()
    """)
    op.execute(
        "ALTER TABLE control_image_transitions "
        "ENABLE ALWAYS TRIGGER dlr_image_transition_append_only"
    )


def downgrade() -> None:
    op.execute("LOCK TABLE control_image_transitions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM control_image_transitions) OR
             EXISTS (SELECT 1 FROM control_cluster_authority
                     WHERE schema_revision = '0088_issue129_image_transition') THEN
            RAISE EXCEPTION 'Image transition prevents downgrade';
          END IF;
        END $$
    """)
    op.drop_table("control_image_transitions")
    op.execute("DROP FUNCTION dlr_image_transition_append_only()")
