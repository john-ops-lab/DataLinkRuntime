"""Bounded, immutable non-secret receipts for HTTP credential operations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0089_issue129_credential_ops"
down_revision = "0088_issue129_image_transition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("worker_credential_audit", sa.Column("request_id", sa.Uuid()))
    op.add_column("worker_credential_audit", sa.Column("request_fingerprint", sa.String(64)))
    op.add_column("worker_credential_audit", sa.Column("result_metadata", JSONB()))
    op.create_unique_constraint(
        "uq_worker_credential_request", "worker_credential_audit", ["request_id"]
    )
    op.create_check_constraint(
        "ck_worker_credential_operation",
        "worker_credential_audit",
        "(request_id IS NULL AND request_fingerprint IS NULL AND result_metadata IS NULL) OR "
        "(request_id IS NOT NULL AND request_id <> '00000000-0000-0000-0000-000000000000'::uuid "
        "AND request_fingerprint IS NOT NULL AND request_fingerprint ~ '^[0-9a-f]{64}$' "
        "AND result_metadata IS NOT NULL AND jsonb_typeof(result_metadata) = 'object' "
        "AND octet_length(result_metadata::text) <= 2048 AND event_kind IN ('rotate', 'revoke'))",
    )
    op.execute("""
        CREATE FUNCTION issue129_credential_receipt_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$ BEGIN
          IF OLD.request_id IS NOT NULL OR
             (TG_OP = 'UPDATE' AND NEW.request_id IS NOT NULL) THEN
            RAISE EXCEPTION 'credential receipt immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION issue129_credential_receipt_immutable() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER issue129_credential_receipt_immutable
        BEFORE UPDATE OR DELETE ON worker_credential_audit
        FOR EACH ROW EXECUTE FUNCTION issue129_credential_receipt_immutable()
    """)
    op.execute(
        "ALTER TABLE worker_credential_audit "
        "ENABLE ALWAYS TRIGGER issue129_credential_receipt_immutable"
    )


def downgrade() -> None:
    raise RuntimeError("HTTP credential receipts require forward repair")
