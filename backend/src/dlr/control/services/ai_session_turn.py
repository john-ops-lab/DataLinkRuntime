"""Durable Assist turns: request HMAC, generation fencing, and visible history."""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import NoReturn, cast

from pydantic import ValidationError
from sqlalchemy import null, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.ai import conversation_rollup, conversation_summary
from dlr.control.ai.conversation_contract import ConversationState, SourceRef, SummarySnapshot
from dlr.control.models.ai_conversation import (
    MAX_CANDIDATE_BYTES,
    MAX_RESPONSE_META_BYTES,
    MAX_VISIBLE_MESSAGE_BYTES,
    AiConversation,
    AiConversationMessage,
)
from dlr.control.schemas.ai import AiAssistRequest, AiAssistResponse, AiRecentMessage
from dlr.control.security import Principal
from dlr.control.services import adapter_access, ai_sessions
from dlr.control.services import ai as ai_service
from dlr.control.services.adapter import domain_error

FAILED_REPLY_PLACEHOLDER = "DLR: This turn did not produce a saved assistant reply."
MAX_PENDING_SECONDS_EXTRA = 30


@dataclass(frozen=True)
class TurnReservation:
    session_id: uuid.UUID
    user_message_id: uuid.UUID
    sequence: int
    generation: int
    idempotency_key: uuid.UUID
    conversation_revision: int
    new_turn: bool


def _conflict(code: str, message: str) -> NoReturn:
    raise domain_error(409, code, message)


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def _request_hmac(payload: AiAssistRequest) -> bytes:
    if not settings.master_key:
        raise domain_error(503, "ai_session_key_unavailable", "AI session request key unavailable")
    serialized = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(
        settings.master_key.encode(),
        b"dlr-ai-session-request-v1\0" + serialized,
        hashlib.sha256,
    ).digest()


def _bounded_visible(text: str) -> None:
    if not text or len(text.encode()) > MAX_VISIBLE_MESSAGE_BYTES:
        raise domain_error(413, "ai_session_message_too_large", "AI session message exceeds limit")


def _cached_response(row: AiConversationMessage) -> AiAssistResponse:
    meta = row.response_meta_json
    if meta is None:
        _conflict("ai_session_replay_unavailable", "Committed response is unavailable")
    try:
        return AiAssistResponse.model_validate(
            {"message": row.content, "candidate": row.candidate_json, **meta}
        )
    except ValidationError:
        _conflict("ai_session_replay_unavailable", "Committed response is invalid")


def _assistant_for(
    session: Session,
    conversation_id: uuid.UUID,
    user_sequence: int,
) -> AiConversationMessage | None:
    return session.scalar(
        select(AiConversationMessage).where(
            AiConversationMessage.conversation_id == conversation_id,
            AiConversationMessage.sequence == user_sequence + 1,
        )
    )


def _require_complete_covered_sources(
    session: Session,
    conversation: AiConversation,
) -> None:
    if not conversation.summary_valid:
        return
    rows = list(
        session.scalars(
            select(AiConversationMessage)
            .where(
                AiConversationMessage.conversation_id == conversation.id,
                AiConversationMessage.sequence <= conversation.summary_covered_through,
            )
            .order_by(AiConversationMessage.sequence)
        )
    )
    if len(rows) != conversation.summary_covered_through or any(
        row.sequence != sequence for sequence, row in enumerate(rows, 1)
    ):
        _conflict("ai_summary_original_missing", "Covered source messages are missing")


def _fill_missing_assistant_slots(session: Session, conversation: AiConversation) -> None:
    rows = list(
        session.scalars(
            select(AiConversationMessage)
            .where(AiConversationMessage.conversation_id == conversation.id)
            .order_by(AiConversationMessage.sequence)
        )
    )
    by_sequence = {row.sequence: row for row in rows}
    if any(sequence not in by_sequence for sequence in range(1, conversation.next_sequence, 2)):
        _conflict("ai_summary_original_missing", "A stored user message is missing")
    for row in rows:
        if row.role != "user" or row.sequence + 1 >= conversation.next_sequence:
            continue
        if row.sequence + 1 in by_sequence:
            continue
        if row.request_status == "pending":
            row.request_status = "failed"
        placeholder = AiConversationMessage(
            conversation_id=conversation.id,
            sequence=row.sequence + 1,
            turn_id=row.turn_id,
            role="assistant",
            content=FAILED_REPLY_PLACEHOLDER,
            source_revision=1,
            generation=row.generation,
            response_meta_json={"provider": "", "model": "", "tool_calls": []},
        )
        session.add(placeholder)
    session.flush()


def _reserve_turn(
    session: Session,
    adapter_id: int,
    payload: AiAssistRequest,
    principal: Principal,
    digest: bytes,
) -> TurnReservation | AiAssistResponse:
    assert payload.session_id is not None
    assert payload.turn_id is not None
    assert payload.idempotency_key is not None
    assert payload.expected_generation is not None
    assert payload.expected_session_revision is not None
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    conversation = ai_sessions.require_owned_session(
        session, adapter_id, payload.session_id, principal, for_update=True
    )
    same_key = session.scalar(
        select(AiConversationMessage).where(
            AiConversationMessage.conversation_id == conversation.id,
            AiConversationMessage.role == "user",
            AiConversationMessage.idempotency_key == payload.idempotency_key,
        )
    )
    if same_key is not None and same_key.turn_id != payload.turn_id:
        _conflict("ai_session_key_conflict", "Idempotency key belongs to another turn")
    user_row = session.scalar(
        select(AiConversationMessage).where(
            AiConversationMessage.conversation_id == conversation.id,
            AiConversationMessage.turn_id == payload.turn_id,
            AiConversationMessage.role == "user",
        )
    )
    new_turn = user_row is None
    if user_row is None:
        if conversation.revision != payload.expected_session_revision:
            _conflict("ai_session_stale_revision", "AI session changed before this turn")
        if payload.regenerate_turn_id is not None:
            _conflict("ai_session_turn_missing", "Regeneration target does not exist")
        if payload.expected_generation != 0:
            _conflict("ai_session_stale_generation", "New turn requires generation zero")
        if conversation.next_sequence > 511:
            _conflict("ai_session_full", "AI session has reached its message limit")
        _bounded_visible(payload.message)
        _fill_missing_assistant_slots(session, conversation)
        user_row = AiConversationMessage(
            conversation_id=conversation.id,
            sequence=conversation.next_sequence,
            turn_id=payload.turn_id,
            role="user",
            content=payload.message,
            generation=1,
            request_status="pending",
            idempotency_key=payload.idempotency_key,
            request_hmac=digest,
            base_version_id=payload.base_version_id,
        )
        session.add(user_row)
        conversation.next_sequence += 2
    else:
        if payload.message != user_row.content:
            _conflict("ai_session_turn_conflict", "Turn message differs from its stored original")
        if user_row.idempotency_key == payload.idempotency_key:
            if payload.expected_generation != user_row.generation - 1:
                _conflict("ai_session_stale_generation", "Turn generation changed")
            if user_row.request_hmac is None or not hmac.compare_digest(
                user_row.request_hmac, digest
            ):
                _conflict("ai_session_key_conflict", "Idempotency key request differs")
            if user_row.request_status == "completed":
                assistant = _assistant_for(session, conversation.id, user_row.sequence)
                if assistant is None or assistant.generation != user_row.generation:
                    _conflict("ai_session_replay_unavailable", "Committed reply is unavailable")
                return _cached_response(assistant)
            if user_row.request_status == "pending":
                stale_after = timedelta(
                    seconds=settings.ai_assist_total_timeout_seconds + MAX_PENDING_SECONDS_EXTRA
                )
                if user_row.updated_at + stale_after > datetime.now(UTC):
                    _conflict("ai_session_in_progress", "This turn is already in progress")
            # A failed, cancelled, or abandoned same-key HTTP attempt retries
            # exactly its frozen request and current generation.
            user_row.request_status = "pending"
            user_row.updated_at = datetime.now(UTC)
        else:
            if conversation.revision != payload.expected_session_revision:
                _conflict("ai_session_stale_revision", "AI session changed before regeneration")
            if payload.regenerate_turn_id != payload.turn_id:
                _conflict("ai_session_turn_conflict", "Use explicit regeneration for an old turn")
            if payload.expected_generation != user_row.generation:
                _conflict("ai_session_stale_generation", "Turn generation changed")
            _require_complete_covered_sources(session, conversation)
            assistant = _assistant_for(session, conversation.id, user_row.sequence)
            if assistant is None:
                _conflict("ai_session_reply_missing", "No prior reply exists to regenerate")
            user_row.generation += 1
            user_row.request_status = "pending"
            user_row.idempotency_key = payload.idempotency_key
            user_row.request_hmac = digest
            user_row.base_version_id = payload.base_version_id
    conversation.revision += 1
    session.flush()
    return TurnReservation(
        session_id=conversation.id,
        user_message_id=user_row.id,
        sequence=user_row.sequence,
        generation=user_row.generation,
        idempotency_key=payload.idempotency_key,
        conversation_revision=conversation.revision,
        new_turn=new_turn,
    )


def _valid_saved_summary(
    conversation: AiConversation,
    rows: list[AiConversationMessage],
    history_through: int,
) -> tuple[SummarySnapshot, ConversationState] | None:
    if not conversation.summary_valid or conversation.summary_covered_through > history_through:
        return None
    covered = conversation.summary_covered_through
    selected = [row for row in rows if row.sequence <= covered]
    if len(selected) != covered or any(
        row.sequence != index for index, row in enumerate(selected, 1)
    ):
        return None
    source_json = [
        SourceRef.model_validate(
            {
                "sequence": row.sequence,
                "revision": row.source_revision,
                "role": row.role,
            }
        ).model_dump(mode="json")
        for row in selected
    ]
    if source_json != conversation.summary_source_revisions:
        return None
    try:
        snapshot = SummarySnapshot.model_validate_json(
            json.dumps(conversation.summary_json), strict=True
        )
        state = ConversationState.model_validate_json(
            json.dumps(conversation.state_json), strict=True
        )
    except (TypeError, ValidationError):
        return None
    if snapshot.covered_through != covered:
        return None
    return snapshot, state


def _prompt_history(
    session: Session,
    reservation: TurnReservation,
    principal: Principal,
    adapter_id: int,
) -> tuple[list[AiRecentMessage], dict[str, object] | None]:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    conversation = ai_sessions.require_owned_session(
        session, adapter_id, reservation.session_id, principal
    )
    user_row = session.get(AiConversationMessage, reservation.user_message_id)
    if (
        conversation.revision != reservation.conversation_revision
        or user_row is None
        or user_row.request_status != "pending"
        or user_row.generation != reservation.generation
        or user_row.idempotency_key != reservation.idempotency_key
    ):
        _conflict("ai_session_stale", "AI session turn changed before Assist")
    rows = list(
        session.scalars(
            select(AiConversationMessage)
            .where(
                AiConversationMessage.conversation_id == conversation.id,
                AiConversationMessage.sequence < reservation.sequence,
            )
            .order_by(AiConversationMessage.sequence)
        )
    )
    if len(rows) != reservation.sequence - 1 or any(
        row.sequence != sequence for sequence, row in enumerate(rows, 1)
    ):
        _conflict("ai_summary_original_missing", "Stored conversation history is incomplete")
    valid = _valid_saved_summary(conversation, rows, reservation.sequence - 1)
    covered = valid[0].covered_through if valid else 0
    history = [
        AiRecentMessage.model_validate({"role": row.role, "content": row.content})
        for row in rows
        if row.sequence > covered
    ]
    context: dict[str, object] | None = None
    if valid is not None:
        snapshot, state = valid
        context = {
            "summary": snapshot.model_dump(mode="json"),
            "active_state": [fact.model_dump(mode="json") for fact in state.active_facts()],
        }
    return history, context


def _current_reservation(
    session: Session,
    reservation: TurnReservation,
    principal: Principal,
    adapter_id: int,
    *,
    require_access: bool = True,
) -> tuple[AiConversation, AiConversationMessage]:
    if require_access:
        adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    conversation = ai_sessions.require_owned_session(
        session, adapter_id, reservation.session_id, principal, for_update=True
    )
    user_row = session.get(AiConversationMessage, reservation.user_message_id)
    if (
        conversation.revision != reservation.conversation_revision
        or user_row is None
        or user_row.conversation_id != conversation.id
        or user_row.generation != reservation.generation
        or user_row.idempotency_key != reservation.idempotency_key
        or user_row.request_status != "pending"
    ):
        _conflict("ai_session_stale", "AI session turn changed while Assist was running")
    return conversation, user_row


def _finish_failure(
    session: Session,
    reservation: TurnReservation,
    principal: Principal,
    adapter_id: int,
) -> None:
    try:
        conversation, user_row = _current_reservation(
            session, reservation, principal, adapter_id, require_access=False
        )
    except Exception:
        # Clear/delete/revocation/newer generation wins. Do not resurrect it.
        session.rollback()
        return
    user_row.request_status = "failed"
    conversation.revision += 1
    session.commit()


def _finish_success(
    session: Session,
    reservation: TurnReservation,
    principal: Principal,
    adapter_id: int,
    response: AiAssistResponse,
) -> AiAssistResponse:
    if response.candidate is not None:
        # Provider compatibility echoes must never turn the request Working
        # Copy configuration into durable conversation data or replay output.
        response = response.model_copy(
            update={
                "candidate": response.candidate.model_copy(
                    update={"requirements": "", "runtime_config": {}}
                )
            }
        )
    _bounded_visible(response.message)
    payload = response.model_dump(mode="json")
    meta = {
        "provider": payload["provider"],
        "model": payload["model"],
        "tool_calls": payload["tool_calls"],
    }
    candidate = payload["candidate"]
    if _json_bytes(meta) > MAX_RESPONSE_META_BYTES or (
        candidate is not None and _json_bytes(candidate) > MAX_CANDIDATE_BYTES
    ):
        raise domain_error(413, "ai_session_result_too_large", "AI session result exceeds limit")
    conversation, user_row = _current_reservation(session, reservation, principal, adapter_id)
    assistant = _assistant_for(session, conversation.id, user_row.sequence)
    if assistant is None:
        assistant = AiConversationMessage(
            conversation_id=conversation.id,
            sequence=user_row.sequence + 1,
            turn_id=user_row.turn_id,
            role="assistant",
            content=response.message,
            generation=reservation.generation,
            source_revision=1,
            response_meta_json=meta,
        )
        if candidate is not None:
            assistant.candidate_json = candidate
        session.add(assistant)
    else:
        # The entire covered range, not only cited messages, depends on this
        # revision. Regeneration and placeholder replacement use one slot.
        conversation_rollup.invalidate_changed_reply(
            session,
            conversation.id,
            assistant.sequence,
            new_revision=assistant.source_revision + 1,
        )
        assistant.content = response.message
        assistant.source_revision += 1
        assistant.generation = reservation.generation
        assistant.response_meta_json = meta
        assistant.candidate_json = (
            candidate if candidate is not None else cast(dict[str, object] | None, null())
        )
    user_row.request_status = "completed"
    conversation.revision += 1
    session.commit()
    return response


def _maybe_rollup(
    session_factory: Callable[[], Session],
    adapter_id: int,
    reservation: TurnReservation,
    principal: Principal,
    hard_deadline: float,
) -> TurnReservation:
    if not reservation.new_turn:
        return reservation
    with session_factory() as session:
        adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
        conversation = ai_sessions.require_owned_session(
            session, adapter_id, reservation.session_id, principal
        )
        if conversation.revision != reservation.conversation_revision:
            return reservation
        previous_through = reservation.sequence - 1
        recent_from = max(1, previous_through - 7)
        if recent_from <= 1:
            return reservation
        setting = ai_service.get_setting(session)
        if setting is None:
            return reservation
        draft = ai_service._setting_draft(setting)
        adapter = ai_service._validate_setting(session, draft)
        api_key = ai_service._resolve_api_key(session, draft.credential_id)
    # Summary is opportunistic and shares the enclosing Assist deadline.
    # Its failure never prevents the current user turn from proceeding.
    try:
        result = conversation_rollup.rollup_before_recent_window(
            session_factory,
            reservation.session_id,
            recent_from_sequence=recent_from,
            draft=draft,
            api_key=api_key,
            adapter=adapter,
            hard_deadline=hard_deadline,
            call_budget=conversation_summary.SummaryCallBudget(),
        )
    except Exception:
        # Internal summary failure must not discard the user's Assist request.
        # The full still-uncovered history is checked against Assist budget.
        return reservation
    if not result.accepted or result.covered_through == 0:
        return reservation
    with session_factory() as session:
        adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
        conversation = ai_sessions.require_owned_session(
            session, adapter_id, reservation.session_id, principal
        )
        row = session.get(AiConversationMessage, reservation.user_message_id)
        if (
            conversation.revision == reservation.conversation_revision + 1
            and row is not None
            and row.request_status == "pending"
            and row.generation == reservation.generation
            and row.idempotency_key == reservation.idempotency_key
        ):
            return replace(reservation, conversation_revision=conversation.revision)
    return reservation


def assist(
    adapter_id: int,
    payload: AiAssistRequest,
    principal: Principal,
    *,
    session_factory: Callable[[], Session] | None = None,
) -> AiAssistResponse:
    """Commit the request fence, call Provider, then CAS the validated result."""
    assert payload.session_id is not None
    assert payload.turn_id is not None
    assert payload.idempotency_key is not None
    assert payload.expected_generation is not None
    factory = session_factory or db.SessionLocal
    hard_deadline = time.monotonic() + settings.ai_assist_total_timeout_seconds
    digest = _request_hmac(payload)
    _bounded_visible(payload.message)
    with factory() as session:
        reservation = _reserve_turn(session, adapter_id, payload, principal, digest)
        if isinstance(reservation, AiAssistResponse):
            return reservation
        session.commit()
    try:
        reservation = _maybe_rollup(factory, adapter_id, reservation, principal, hard_deadline)
        if time.monotonic() >= hard_deadline:
            raise domain_error(408, "ai_session_deadline", "AI session deadline expired")
        with factory() as session:
            history, context = _prompt_history(session, reservation, principal, adapter_id)
            provider_payload = payload.model_copy(update={"recent_messages": history})
            result = ai_service.assist(
                session,
                adapter_id,
                provider_payload,
                conversation_context=context,
                hard_deadline=hard_deadline,
            )
        with factory() as session:
            return _finish_success(session, reservation, principal, adapter_id, result)
    except Exception:
        with factory() as session:
            _finish_failure(session, reservation, principal, adapter_id)
        raise
