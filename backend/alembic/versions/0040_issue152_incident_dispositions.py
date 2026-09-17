"""Add durable, idempotent infrastructure Incident dispositions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_issue152_dispositions"
down_revision: str | None = "0039_issue134_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_incident_dispositions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=False),
        sa.Column("execution_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("actor_kind", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("from_generation", sa.BigInteger(), nullable=True),
        sa.Column("to_generation", sa.BigInteger(), nullable=True),
        sa.Column("from_outbox_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_outbox_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("execution_status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_execution_incident_dispositions_request_hash",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('superadmin', 'account')",
            name="ck_execution_incident_dispositions_actor_kind",
        ),
        sa.CheckConstraint(
            "(actor_kind = 'account' AND user_id IS NOT NULL) OR "
            "(actor_kind = 'superadmin' AND user_id IS NULL)",
            name="ck_execution_incident_dispositions_actor_identity",
        ),
        sa.CheckConstraint(
            "action IN ('recover', 'terminate')",
            name="ck_execution_incident_dispositions_action",
        ),
        sa.CheckConstraint(
            "reason_code IN ('capacity_repaired', 'routing_repaired', "
            "'operator_cancel', 'verified_terminal')",
            name="ck_execution_incident_dispositions_reason_code",
        ),
        sa.CheckConstraint(
            "from_generation IS NULL OR from_generation >= 1",
            name="ck_execution_incident_dispositions_from_generation",
        ),
        sa.CheckConstraint(
            "to_generation IS NULL OR to_generation >= 1",
            name="ck_execution_incident_dispositions_to_generation",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["execution_infrastructure_incidents.id"],
            ondelete="CASCADE",
            name="fk_incident_disposition_incident",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            ondelete="CASCADE",
            name="fk_incident_disposition_execution",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT", name="fk_incident_disposition_user"
        ),
        sa.ForeignKeyConstraint(
            ["from_outbox_id"],
            ["execution_outbox.id"],
            ondelete="SET NULL",
            name="fk_incident_disposition_from_outbox",
        ),
        sa.ForeignKeyConstraint(
            ["to_outbox_id"],
            ["execution_outbox.id"],
            ondelete="SET NULL",
            name="fk_incident_disposition_to_outbox",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "incident_id",
            "idempotency_key",
            name="uq_execution_incident_dispositions_incident_key",
        ),
    )
    op.create_index(
        "ix_execution_incident_dispositions_incident_page",
        "execution_incident_dispositions",
        ["incident_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_incident_dispositions_incident_page",
        table_name="execution_incident_dispositions",
    )
    op.drop_table("execution_incident_dispositions")
