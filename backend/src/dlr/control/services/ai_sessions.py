"""Opt-in AI sessions owned by an authenticated principal, never by a token value."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dlr.control.ai.conversation_contract import ConversationState
from dlr.control.models.ai_conversation import AiConversation, AiConversationMessage
from dlr.control.schemas.ai_session import (
    AiSessionDetail,
    AiSessionList,
    AiSessionMessage,
    AiSessionSummary,
)
from dlr.control.security import Principal
from dlr.control.services.adapter import domain_error

SESSION_RETENTION_DAYS = 30
MAX_LIST_LIMIT = 100


def cleanup_expired_sessions(
    session: Session, *, now: datetime | None = None, batch_size: int = 100
) -> int:
    """Purge one bounded expiry batch; locked in-flight sessions wait for a later tick."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    cutoff = now or datetime.now(UTC)
    ids = list(
        session.scalars(
            select(AiConversation.id)
            .where(AiConversation.expires_at <= cutoff)
            .order_by(AiConversation.expires_at, AiConversation.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if not ids:
        return 0
    result = cast(
        Any,
        session.execute(
            delete(AiConversation).where(
                AiConversation.id.in_(ids), AiConversation.expires_at <= cutoff
            )
        ),
    )
    session.commit()
    return int(result.rowcount or 0)


def _owner(principal: Principal) -> tuple[str, int | None]:
    if principal.kind == "superadmin":
        return "deployment", None
    if principal.user_id is None:
        raise domain_error(401, "account_session_required", "Account identity is required")
    return "account", principal.user_id


def _not_found() -> NoReturn:
    raise domain_error(404, "ai_session_not_found", "AI session not found")


def require_owned_session(
    session: Session,
    adapter_id: int,
    session_id: uuid.UUID,
    principal: Principal,
    *,
    for_update: bool = False,
) -> AiConversation:
    """Call after a fresh Adapter edit check; hide other owners and expired rows."""
    query = select(AiConversation).where(
        AiConversation.id == session_id,
        AiConversation.adapter_id == adapter_id,
    )
    if for_update:
        query = query.with_for_update()
    conversation = session.scalar(query)
    owner_kind, account_user_id = _owner(principal)
    if (
        conversation is None
        or conversation.owner_kind != owner_kind
        or conversation.account_user_id != account_user_id
        or conversation.expires_at <= datetime.now(UTC)
    ):
        _not_found()
    return conversation


def _summary(conversation: AiConversation) -> AiSessionSummary:
    return AiSessionSummary(
        id=conversation.id,
        adapter_id=conversation.adapter_id,
        revision=conversation.revision,
        expires_at=conversation.expires_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def create_session(session: Session, adapter_id: int, principal: Principal) -> AiSessionSummary:
    owner_kind, account_user_id = _owner(principal)
    conversation = AiConversation(
        adapter_id=adapter_id,
        owner_kind=owner_kind,
        account_user_id=account_user_id,
        expires_at=datetime.now(UTC) + timedelta(days=SESSION_RETENTION_DAYS),
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return _summary(conversation)


def list_sessions(
    session: Session,
    adapter_id: int,
    principal: Principal,
    *,
    limit: int = 50,
) -> AiSessionList:
    owner_kind, account_user_id = _owner(principal)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise domain_error(422, "ai_session_limit_invalid", "Invalid AI session list limit")
    conversations = session.scalars(
        select(AiConversation)
        .where(
            AiConversation.adapter_id == adapter_id,
            AiConversation.owner_kind == owner_kind,
            AiConversation.account_user_id == account_user_id
            if account_user_id is not None
            else AiConversation.account_user_id.is_(None),
            AiConversation.expires_at > datetime.now(UTC),
        )
        .order_by(AiConversation.updated_at.desc(), AiConversation.id.desc())
        .limit(limit)
    )
    return AiSessionList(sessions=[_summary(item) for item in conversations])


def read_session(
    session: Session,
    adapter_id: int,
    session_id: uuid.UUID,
    principal: Principal,
) -> AiSessionDetail:
    conversation = require_owned_session(session, adapter_id, session_id, principal)
    rows = session.scalars(
        select(AiConversationMessage)
        .where(AiConversationMessage.conversation_id == conversation.id)
        .order_by(AiConversationMessage.sequence)
    )
    messages = [
        AiSessionMessage.model_validate(
            {
                "sequence": row.sequence,
                "turn_id": row.turn_id,
                "role": row.role,
                "content": row.content,
                "source_revision": row.source_revision,
                "generation": row.generation,
                "request_status": row.request_status,
            }
        )
        for row in rows
    ]
    return AiSessionDetail(
        **_summary(conversation).model_dump(),
        messages=messages,
        summary_covered_through=conversation.summary_covered_through,
        summary_valid=conversation.summary_valid,
    )


def clear_session(
    session: Session,
    adapter_id: int,
    session_id: uuid.UUID,
    principal: Principal,
) -> AiSessionDetail:
    conversation = require_owned_session(
        session, adapter_id, session_id, principal, for_update=True
    )
    session.execute(
        delete(AiConversationMessage).where(AiConversationMessage.conversation_id == session_id)
    )
    conversation.revision += 1
    conversation.next_sequence = 1
    conversation.state_json = ConversationState(revision=0).model_dump(mode="json")
    conversation.summary_json = None
    conversation.summary_valid = False
    conversation.summary_covered_from = 0
    conversation.summary_covered_through = 0
    conversation.summary_source_revisions = []
    session.commit()
    session.refresh(conversation)
    return read_session(session, adapter_id, session_id, principal)


def delete_session(
    session: Session,
    adapter_id: int,
    session_id: uuid.UUID,
    principal: Principal,
) -> None:
    conversation = require_owned_session(
        session, adapter_id, session_id, principal, for_update=True
    )
    session.delete(conversation)
    session.commit()
