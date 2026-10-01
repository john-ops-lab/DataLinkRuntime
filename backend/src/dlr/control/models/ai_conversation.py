"""Bounded, opt-in AI conversation storage; never a Working Copy authority."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base

# Service-layer create/update paths must enforce these same limits before DB
# writes; the 0045 migration freezes their corresponding SQL checks.
MAX_CONVERSATION_MESSAGES = 512
MAX_CONVERSATION_RETENTION_DAYS = 90
MAX_VISIBLE_MESSAGE_BYTES = 16384
MAX_STATE_BYTES = 32768
MAX_SUMMARY_BYTES = 16384
MAX_SOURCE_REVISIONS_BYTES = 32768
MAX_RESPONSE_META_BYTES = 16384
MAX_CANDIDATE_BYTES = 131072


class AiConversation(Base):
    """Owner-scoped conversation; account and deployment spaces are disjoint."""

    __tablename__ = "ai_conversations"
    __table_args__ = (
        CheckConstraint(
            "(owner_kind = 'account' AND account_user_id IS NOT NULL) OR "
            "(owner_kind = 'deployment' AND account_user_id IS NULL)",
            name="ck_ai_conversations_owner",
        ),
        CheckConstraint("revision >= 0", name="ck_ai_conversations_revision"),
        CheckConstraint(
            "next_sequence BETWEEN 1 AND 513", name="ck_ai_conversations_next_sequence"
        ),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + INTERVAL '90 days'",
            name="ck_ai_conversations_expiry",
        ),
        CheckConstraint(
            "jsonb_typeof(state_json) = 'object' AND "
            "octet_length(state_json::text) <= 32768 AND "
            "state_json - ARRAY['revision', 'facts']::text[] = '{}'::jsonb",
            name="ck_ai_conversations_state_bound",
        ),
        CheckConstraint(
            "summary_json IS NULL OR "
            "(jsonb_typeof(summary_json) = 'object' AND "
            "octet_length(summary_json::text) <= 16384 AND "
            "summary_json - ARRAY['version', 'text', 'sources', 'covered_through']::text[] "
            "= '{}'::jsonb)",
            name="ck_ai_conversations_summary_bound",
        ),
        CheckConstraint(
            "jsonb_typeof(summary_source_revisions) = 'array' AND "
            "octet_length(summary_source_revisions::text) <= 32768 AND "
            "jsonb_array_length(summary_source_revisions) <= 512",
            name="ck_ai_conversations_sources_bound",
        ),
        CheckConstraint(
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
        Index(
            "ix_ai_conversations_account_list",
            "account_user_id",
            "adapter_id",
            "updated_at",
            "id",
            postgresql_where=text("owner_kind = 'account'"),
        ),
        Index(
            "ix_ai_conversations_deployment_list",
            "adapter_id",
            "updated_at",
            "id",
            postgresql_where=text("owner_kind = 'deployment'"),
        ),
        Index("ix_ai_conversations_expiry", "expires_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("adapters.id", ondelete="CASCADE"), nullable=False
    )
    owner_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    account_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE")
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    # Reserve user/assistant sequence slots together when a new turn is made.
    next_sequence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    state_json: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"revision": 0, "facts": []},
        server_default=text("jsonb_build_object('revision', 0, 'facts', jsonb_build_array())"),
    )
    summary_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    summary_valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    summary_covered_from: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    summary_covered_through: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # Snapshot every covered message's sequence/revision, not merely citations.
    summary_source_revisions: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AiConversationMessage(Base):
    """One user row and at most one replaceable assistant row per turn."""

    __tablename__ = "ai_conversation_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_ai_messages_sequence"),
        UniqueConstraint("conversation_id", "turn_id", "role", name="uq_ai_messages_turn_role"),
        CheckConstraint("sequence BETWEEN 1 AND 512", name="ck_ai_messages_sequence"),
        CheckConstraint(
            "(role = 'user' AND mod(sequence, 2) = 1) OR "
            "(role = 'assistant' AND mod(sequence, 2) = 0)",
            name="ck_ai_messages_role_sequence",
        ),
        CheckConstraint("source_revision >= 1", name="ck_ai_messages_source_revision"),
        CheckConstraint("generation >= 1", name="ck_ai_messages_generation"),
        CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 16384", name="ck_ai_messages_content_bound"
        ),
        CheckConstraint(
            "response_meta_json IS NULL OR "
            "(jsonb_typeof(response_meta_json) = 'object' AND "
            "octet_length(response_meta_json::text) <= 16384 AND "
            "response_meta_json - ARRAY['provider', 'model', 'tool_calls']::text[] "
            "= '{}'::jsonb)",
            name="ck_ai_messages_response_bound",
        ),
        CheckConstraint(
            "candidate_json IS NULL OR "
            "(jsonb_typeof(candidate_json) = 'object' AND "
            "octet_length(candidate_json::text) <= 131072 AND "
            "candidate_json - ARRAY['summary', 'code', 'required_secret_keys', "
            "'requirements', 'runtime_config']::text[] = '{}'::jsonb)",
            name="ck_ai_messages_candidate_bound",
        ),
        CheckConstraint(
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
        Index(
            "uq_ai_messages_current_key",
            "conversation_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("role = 'user'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # These fields live on the sole user row for the turn. Advancing generation
    # replaces the active key/HMAC; an old key can never identify a new attempt.
    request_status: Mapped[str | None] = mapped_column(String(16))
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    request_hmac: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    base_version_id: Mapped[int | None] = mapped_column(BigInteger)
    # Only the current validated assistant reply is retained. A successful
    # regeneration updates this same logical row/sequence and increments its
    # source_revision; unsuccessful regeneration leaves the row unchanged.
    response_meta_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    candidate_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
