"""Add bounded, owner-scoped AI conversations and visible message turns.

Revision ID: 0045_issue151_ai_conversations
Revises: 0044_issue161_cache_admin
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_issue151_ai_conversations"
down_revision = "0044_issue161_cache_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_conversations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "adapter_id",
            sa.BigInteger(),
            sa.ForeignKey("adapters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_kind", sa.String(16), nullable=False),
        sa.Column(
            "account_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column(
            "state_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(
                "jsonb_build_object('revision', 0, 'facts', jsonb_build_array())"
            ),
        ),
        sa.Column("summary_json", postgresql.JSONB()),
        sa.Column("summary_valid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("summary_covered_from", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("summary_covered_through", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "summary_source_revisions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(owner_kind = 'account' AND account_user_id IS NOT NULL) OR "
            "(owner_kind = 'deployment' AND account_user_id IS NULL)",
            name="ck_ai_conversations_owner",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_ai_conversations_revision"),
        sa.CheckConstraint(
            "next_sequence BETWEEN 1 AND 513", name="ck_ai_conversations_next_sequence"
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '90 days'",
            name="ck_ai_conversations_expiry",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(state_json) = 'object' AND "
            "octet_length(state_json::text) <= 32768 AND "
            "state_json - ARRAY['revision', 'facts']::text[] = '{}'::jsonb",
            name="ck_ai_conversations_state_bound",
        ),
        sa.CheckConstraint(
            "summary_json IS NULL OR "
            "(jsonb_typeof(summary_json) = 'object' AND "
            "octet_length(summary_json::text) <= 16384 AND "
            "summary_json - ARRAY['version', 'text', 'sources', 'covered_through']::text[] "
            "= '{}'::jsonb)",
            name="ck_ai_conversations_summary_bound",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(summary_source_revisions) = 'array' AND "
            "octet_length(summary_source_revisions::text) <= 32768 AND "
            "jsonb_array_length(summary_source_revisions) <= 512",
            name="ck_ai_conversations_sources_bound",
        ),
        sa.CheckConstraint(
            "(summary_json IS NULL AND NOT summary_valid AND "
            "summary_covered_from = 0 AND summary_covered_through = 0 AND "
            "jsonb_array_length(summary_source_revisions) = 0) OR "
            "(summary_json IS NOT NULL AND summary_covered_from >= 1 AND "
            "summary_covered_through >= summary_covered_from AND "
            "summary_covered_through < next_sequence AND "
            "jsonb_array_length(summary_source_revisions) = "
            "summary_covered_through - summary_covered_from + 1)",
            name="ck_ai_conversations_summary_range",
        ),
    )
    op.create_index(
        "ix_ai_conversations_account_list",
        "ai_conversations",
        ["account_user_id", "adapter_id", "updated_at", "id"],
        postgresql_where=sa.text("owner_kind = 'account'"),
    )
    op.create_index(
        "ix_ai_conversations_deployment_list",
        "ai_conversations",
        ["adapter_id", "updated_at", "id"],
        postgresql_where=sa.text("owner_kind = 'deployment'"),
    )
    op.create_index("ix_ai_conversations_expiry", "ai_conversations", ["expires_at", "id"])
    op.create_table(
        "ai_conversation_messages",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("turn_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("request_status", sa.String(16)),
        sa.Column("idempotency_key", sa.Uuid(as_uuid=True)),
        sa.Column("request_hmac", sa.LargeBinary(32)),
        sa.Column("base_version_id", sa.BigInteger()),
        sa.Column("response_meta_json", postgresql.JSONB()),
        sa.Column("candidate_json", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_ai_messages_sequence"),
        sa.UniqueConstraint("conversation_id", "turn_id", "role", name="uq_ai_messages_turn_role"),
        sa.CheckConstraint("sequence BETWEEN 1 AND 512", name="ck_ai_messages_sequence"),
        sa.CheckConstraint(
            "(role = 'user' AND mod(sequence, 2) = 1) OR "
            "(role = 'assistant' AND mod(sequence, 2) = 0)",
            name="ck_ai_messages_role_sequence",
        ),
        sa.CheckConstraint("source_revision >= 1", name="ck_ai_messages_source_revision"),
        sa.CheckConstraint("generation >= 1", name="ck_ai_messages_generation"),
        sa.CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 16384", name="ck_ai_messages_content_bound"
        ),
        sa.CheckConstraint(
            "response_meta_json IS NULL OR "
            "(jsonb_typeof(response_meta_json) = 'object' AND "
            "octet_length(response_meta_json::text) <= 16384 AND "
            "response_meta_json - ARRAY['provider', 'model', 'tool_calls']::text[] "
            "= '{}'::jsonb)",
            name="ck_ai_messages_response_bound",
        ),
        sa.CheckConstraint(
            "candidate_json IS NULL OR "
            "(jsonb_typeof(candidate_json) = 'object' AND "
            "octet_length(candidate_json::text) <= 131072 AND "
            "candidate_json - ARRAY['summary', 'code', 'required_secret_keys', "
            "'requirements', 'runtime_config']::text[] = '{}'::jsonb)",
            name="ck_ai_messages_candidate_bound",
        ),
        sa.CheckConstraint(
            "(role = 'user' AND request_status IN "
            "('pending', 'completed', 'failed', 'cancelled') AND "
            "idempotency_key IS NOT NULL AND request_hmac IS NOT NULL AND "
            "octet_length(request_hmac) = 32 AND source_revision = 1 AND "
            "response_meta_json IS NULL AND candidate_json IS NULL) OR "
            "(role = 'assistant' AND request_status IS NULL AND "
            "idempotency_key IS NULL AND request_hmac IS NULL AND "
            "base_version_id IS NULL AND response_meta_json IS NOT NULL)",
            name="ck_ai_messages_role_payload",
        ),
    )
    op.create_index(
        "uq_ai_messages_current_key",
        "ai_conversation_messages",
        ["conversation_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("role = 'user'"),
    )


def downgrade() -> None:
    # Never silently discard opt-in conversation history during code rollback.
    # A deployment decision must explicitly clear/export it first.
    has_data = op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM ai_conversations)"))
    if has_data:
        raise RuntimeError("Cannot downgrade while AI conversations exist")
    op.drop_index("uq_ai_messages_current_key", table_name="ai_conversation_messages")
    op.drop_table("ai_conversation_messages")
    op.drop_index("ix_ai_conversations_expiry", table_name="ai_conversations")
    op.drop_index("ix_ai_conversations_deployment_list", table_name="ai_conversations")
    op.drop_index("ix_ai_conversations_account_list", table_name="ai_conversations")
    op.drop_table("ai_conversations")
