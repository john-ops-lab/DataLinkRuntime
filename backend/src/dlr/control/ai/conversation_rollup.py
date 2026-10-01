"""Optimistic, source-complete persistence for conversation summaries and state."""

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.ai import conversation_summary, providers
from dlr.control.ai.conversation_contract import (
    ConversationMessage,
    ConversationState,
    FactAdditionProposal,
    FactRevocationProposal,
    SourceRef,
    StateFact,
    SummarySnapshot,
)
from dlr.control.models.ai_conversation import (
    MAX_SOURCE_REVISIONS_BYTES,
    MAX_STATE_BYTES,
    MAX_SUMMARY_BYTES,
    AiConversation,
    AiConversationMessage,
)
from dlr.control.schemas.ai import AiSettingDraft

MAX_ROLLUP_MESSAGES = 8  # Four complete user/assistant turns per Assist.


@dataclass(frozen=True)
class RollupPlan:
    conversation_id: uuid.UUID
    conversation_revision: int
    previous_cursor: int
    previous_valid: bool
    previous: SummarySnapshot | None
    state: ConversationState
    pending: tuple[ConversationMessage, ...]
    covered_revisions: tuple[SourceRef, ...]


@dataclass(frozen=True)
class RollupResult:
    accepted: bool
    error_code: str | None = None
    covered_through: int = 0


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def _sources(rows: list[AiConversationMessage]) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef.model_validate(
            {"sequence": row.sequence, "revision": row.source_revision, "role": row.role}
        )
        for row in rows
    )


def _complete(rows: list[AiConversationMessage], through: int) -> bool:
    return len(rows) == through and all(row.sequence == index for index, row in enumerate(rows, 1))


def prepare_rollup(
    session: Session, conversation_id: uuid.UUID, *, recent_from_sequence: int
) -> tuple[RollupPlan | None, str | None]:
    """Select the earliest bounded, complete turns before the recent window."""
    conversation = session.get(AiConversation, conversation_id)
    if conversation is None:
        return None, "ai_conversation_missing"
    if conversation.expires_at.timestamp() <= time.time():
        return None, "ai_conversation_expired"
    boundary = min(recent_from_sequence - 1, conversation.next_sequence - 1)
    if boundary < 2:
        return None, None
    rows = list(
        session.scalars(
            select(AiConversationMessage)
            .where(
                AiConversationMessage.conversation_id == conversation_id,
                AiConversationMessage.sequence <= boundary,
            )
            .order_by(AiConversationMessage.sequence)
        )
    )
    stored_valid = bool(conversation.summary_valid)
    old_valid = stored_valid
    previous: SummarySnapshot | None = None
    try:
        if old_valid:
            previous = SummarySnapshot.model_validate_json(
                json.dumps(conversation.summary_json), strict=True
            )
            old_through = conversation.summary_covered_through
            if (
                conversation.summary_covered_from != 1
                or previous.covered_through != old_through
                or old_through > boundary
                or not _complete([row for row in rows if row.sequence <= old_through], old_through)
                or conversation.summary_source_revisions
                != [
                    source.model_dump(mode="json")
                    for source in _sources([row for row in rows if row.sequence <= old_through])
                ]
            ):
                old_valid = False
                previous = None
    except (ValidationError, TypeError):
        old_valid = False
        previous = None
    cursor = previous.covered_through if previous else 0
    through = min(boundary, cursor + MAX_ROLLUP_MESSAGES)
    through -= through % 2
    if cursor >= through:
        return None, None
    rows = [row for row in rows if row.sequence <= through]
    if not _complete(rows, through):
        return None, "ai_summary_original_missing"
    sources = _sources(rows)
    try:
        state = ConversationState.model_validate_json(
            json.dumps(conversation.state_json), strict=True
        )
    except ValidationError:
        return None, "ai_state_invalid"
    if not old_valid:
        state = ConversationState(revision=0)
    pending = tuple(
        ConversationMessage(source=source, text=row.content)
        for source, row in zip(sources[cursor:], rows[cursor:], strict=True)
    )
    return (
        RollupPlan(
            conversation_id,
            conversation.revision,
            conversation.summary_covered_through,
            stored_valid,
            previous,
            state,
            pending,
            sources,
        ),
        None,
    )


def _proposed_state(
    previous: ConversationState,
    pending: tuple[ConversationMessage, ...],
    additions: tuple[FactAdditionProposal, ...],
    revocations: tuple[FactRevocationProposal, ...],
) -> ConversationState:
    evidence = {item.source: item.text for item in pending}
    existing = {fact.id: fact for fact in previous.facts}
    new_ids = [item.id for item in additions]
    revoked_ids = [item.fact_id for item in revocations]
    if len(set(new_ids)) != len(new_ids) or len(set(revoked_ids)) != len(revoked_ids):
        raise ValueError("duplicate state change")
    if set(new_ids) & set(existing):
        raise ValueError("existing state fact id")
    for source, quote in (
        *((item.source, item.evidence_quote) for item in additions),
        *((item.source, item.evidence_quote) for item in revocations),
    ):
        if source not in evidence or quote not in evidence[source]:
            raise ValueError("state evidence must exactly quote a pending source")
    if not additions and not revocations:
        return previous
    introduction_revision = previous.revision + 1
    new_facts = tuple(
        StateFact(
            id=item.id,
            kind=item.kind,
            confirmation_level=item.confirmation_level,
            text=item.text,
            source=item.source,
            introduced_revision=introduction_revision,
        )
        for item in additions
    )
    all_facts = previous.facts + new_facts
    active_by_id = {fact.id: fact for fact in all_facts}
    for item in revocations:
        fact = active_by_id.get(item.fact_id)
        if (
            fact is None
            or fact.revoked_by is not None
            or item.source.role != "user"
            or item.source.sequence <= fact.source.sequence
        ):
            raise ValueError("only a later user source can revoke an active fact")
    # A fact introduced and revoked inside one pending range still retains both
    # revisions. A single model output never chooses these service revisions.
    revocation_revision = introduction_revision + 1 if additions else introduction_revision
    revision = revocation_revision if revocations else introduction_revision
    facts = tuple(
        fact.model_copy(
            update={
                "revoked_by": next(item.source for item in revocations if item.fact_id == fact.id),
                "revoked_revision": revocation_revision,
            }
        )
        if fact.id in revoked_ids
        else fact
        for fact in all_facts
    )
    return ConversationState(revision=revision, facts=facts)


def persist_rollup(
    session: Session, plan: RollupPlan, attempt: conversation_summary.SummaryAttempt
) -> RollupResult:
    """Caller commits this transaction; no row changes occur on rejection."""
    if not attempt.accepted or attempt.snapshot is None:
        return RollupResult(False, attempt.error_code or "ai_summary_invalid")
    conversation = session.scalar(
        select(AiConversation).where(AiConversation.id == plan.conversation_id).with_for_update()
    )
    if conversation is None or conversation.expires_at.timestamp() <= time.time():
        return RollupResult(False, "ai_conversation_expired")
    if (
        conversation.revision != plan.conversation_revision
        or conversation.summary_covered_through != plan.previous_cursor
        or conversation.summary_valid != plan.previous_valid
    ):
        return RollupResult(False, "ai_summary_stale")
    rows = list(
        session.scalars(
            select(AiConversationMessage)
            .where(
                AiConversationMessage.conversation_id == plan.conversation_id,
                AiConversationMessage.sequence <= len(plan.covered_revisions),
            )
            .order_by(AiConversationMessage.sequence)
        )
    )
    if not _complete(rows, len(plan.covered_revisions)) or _sources(rows) != plan.covered_revisions:
        return RollupResult(False, "ai_summary_source_changed")
    snapshot = attempt.snapshot
    if snapshot.covered_through != len(plan.covered_revisions):
        return RollupResult(False, "ai_summary_range_invalid")
    permitted_sources = set(plan.covered_revisions)
    if not set(snapshot.sources) <= permitted_sources:
        return RollupResult(False, "ai_summary_source_changed")
    try:
        state = _proposed_state(plan.state, plan.pending, attempt.additions, attempt.revocations)
    except (ValueError, ValidationError):
        return RollupResult(False, "ai_summary_state_invalid")
    summary_json = snapshot.model_dump(mode="json")
    state_json = state.model_dump(mode="json")
    source_json = [source.model_dump(mode="json") for source in plan.covered_revisions]
    if (
        _json_size(summary_json) > MAX_SUMMARY_BYTES
        or _json_size(state_json) > MAX_STATE_BYTES
        or _json_size(source_json) > MAX_SOURCE_REVISIONS_BYTES
    ):
        return RollupResult(False, "ai_summary_size_exceeded")
    conversation.summary_json = summary_json
    conversation.summary_valid = True
    conversation.summary_covered_from = 1
    conversation.summary_covered_through = snapshot.covered_through
    conversation.summary_source_revisions = source_json
    conversation.state_json = state_json
    conversation.revision += 1
    session.flush()
    return RollupResult(True, covered_through=snapshot.covered_through)


def invalidate_changed_reply(
    session: Session, conversation_id: uuid.UUID, sequence: int, *, new_revision: int
) -> bool:
    """Call in the same transaction that replaces a reply; reject missing originals."""
    conversation = session.scalar(
        select(AiConversation).where(AiConversation.id == conversation_id).with_for_update()
    )
    if conversation is None:
        raise ValueError("conversation missing")
    if sequence < 1 or sequence >= conversation.next_sequence:
        raise ValueError("reply sequence outside conversation")
    row = session.scalar(
        select(AiConversationMessage).where(
            AiConversationMessage.conversation_id == conversation_id,
            AiConversationMessage.sequence == sequence,
        )
    )
    if row is None or row.role != "assistant" or new_revision <= row.source_revision:
        raise ValueError("original reply missing or revision not increasing")
    if conversation.summary_valid and sequence <= conversation.summary_covered_through:
        conversation.summary_valid = False
        conversation.state_json = ConversationState(revision=0).model_dump(mode="json")
        conversation.revision += 1
        return True
    return False


def rollup_before_recent_window(
    session_factory: Callable[[], Session],
    conversation_id: uuid.UUID,
    *,
    recent_from_sequence: int,
    draft: AiSettingDraft,
    api_key: str | None,
    adapter: providers.ProviderAdapter,
    hard_deadline: float,
    call_budget: conversation_summary.SummaryCallBudget,
) -> RollupResult:
    """Read and call Provider without a lock; commit only a fully validated CAS."""
    with session_factory() as session:
        plan, error = prepare_rollup(
            session, conversation_id, recent_from_sequence=recent_from_sequence
        )
    if error:
        return RollupResult(False, error)
    if plan is None:
        return RollupResult(True)
    attempt = conversation_summary.summarize_pending(
        draft=draft,
        api_key=api_key,
        adapter=adapter,
        previous=plan.previous,
        previous_state=plan.state,
        pending=plan.pending,
        hard_deadline=hard_deadline,
        call_budget=call_budget,
    )
    if not attempt.accepted:
        return RollupResult(False, attempt.error_code)
    if time.monotonic() >= hard_deadline:
        return RollupResult(False, "ai_summary_deadline")
    with session_factory() as session:
        result = persist_rollup(session, plan, attempt)
        if result.accepted:
            session.commit()
        return result
