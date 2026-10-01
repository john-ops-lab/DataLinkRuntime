"""Deterministic #151 summary/state boundaries; no persistence or live model."""

import json

import pytest
from pydantic import ValidationError

from dlr.common.config import settings
from dlr.control.ai import conversation_summary, providers
from dlr.control.ai.conversation_contract import (
    ConversationMessage,
    ConversationState,
    SourceRef,
    StateFact,
    SummarySnapshot,
)
from dlr.control.schemas.ai import AiSettingDraft


def _draft() -> AiSettingDraft:
    return AiSettingDraft(
        provider="custom_openai_compatible",
        base_url="http://fake-provider.invalid",
        model="unlisted-model",
        credential_id=None,
        reasoning_mode="default",
        reasoning_effort=None,
    )


def _source(sequence: int, role: str = "user", revision: int = 1) -> SourceRef:
    return SourceRef.model_validate({"sequence": sequence, "role": role, "revision": revision})


def _pending() -> tuple[ConversationMessage, ...]:
    return (
        ConversationMessage(source=_source(2), text="Do not add dependencies."),
        ConversationMessage(
            source=_source(3, "assistant"), text="I will keep dependencies unchanged."
        ),
    )


def _previous() -> SummarySnapshot:
    return SummarySnapshot(
        version=1, text="User wants a Python adapter.", sources=(_source(1),), covered_through=1
    )


def _output(**overrides: object) -> str:
    payload: dict[str, object] = {
        "version": 1,
        "text": "User wants a Python adapter without new dependencies.",
        "sources": [_source(1).model_dump(), _source(2).model_dump()],
        "covered_through": 3,
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_summary_builder_contains_only_required_sources_and_no_assist_contract() -> None:
    messages = conversation_summary.build_summary_messages(_previous(), _pending())
    assert [message["role"] for message in messages] == ["system", "user"]
    system = str(messages[0]["content"])
    request = json.loads(str(messages[1]["content"]))
    assert "AiModelOutput" not in system
    assert "candidate" not in system.lower()
    assert "working copy" not in system.lower()
    assert "working_copy" not in json.dumps(request).lower()
    assert "tool_results" not in request
    assert set(request) == {
        "previous_summary",
        "pending_messages",
        "coverage_after",
        "coverage_through",
    }


def test_summary_uses_existing_deadline_budget_and_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    clock = iter([20.0, 21.0])
    monkeypatch.setattr(conversation_summary.time, "monotonic", lambda: next(clock))

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        captured.update(kwargs)
        return _output(), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    result = conversation_summary.summarize_pending(
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        previous=_previous(),
        pending=_pending(),
        hard_deadline=30.0,
        call_budget=conversation_summary.SummaryCallBudget(),
    )
    assert result.accepted and result.snapshot is not None
    assert result.snapshot.covered_through == 3
    assert captured["tools"] is None
    assert captured["image_input"] is False
    assert captured["timeout_seconds"] == 9.0


def test_summary_has_own_timeout_and_one_call_per_assist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        observed.append(float(kwargs["timeout_seconds"]))
        return _output(), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    monkeypatch.setattr(settings, "ai_summary_timeout_seconds", 12.0)
    budget = conversation_summary.SummaryCallBudget()
    arguments = {
        "draft": _draft(),
        "api_key": None,
        "adapter": providers.get_provider("custom_openai_compatible"),
        "previous": _previous(),
        "pending": _pending(),
        "hard_deadline": conversation_summary.time.monotonic() + 60,
        "call_budget": budget,
    }
    first = conversation_summary.summarize_pending(**arguments)
    second = conversation_summary.summarize_pending(**arguments)
    assert first.accepted
    assert second.error_code == "ai_summary_call_budget"
    assert second.snapshot is arguments["previous"]
    assert budget.calls_used == 1
    assert observed == [12.0]


@pytest.mark.parametrize(
    ("response", "tool_calls"),
    [
        (_output(), [providers.NormalizedToolCall("call-1", "dlr_docs_list", "{}")]),
        (_output(candidate={"code": "bad"}), None),
        (_output(sources=[]), None),
        (_output(sources=[_source(999).model_dump()]), None),
        (_output(sources=[_source(2, revision=99).model_dump()]), None),
        (_output(covered_through=4), None),
        ("not-json", None),
    ],
)
def test_invalid_summary_keeps_previous_coverage(
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    tool_calls: list[providers.NormalizedToolCall] | None,
) -> None:
    old = _previous()
    monkeypatch.setattr(providers, "chat_assist", lambda *args, **kwargs: (response, tool_calls))
    result = conversation_summary.summarize_pending(
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        previous=old,
        pending=_pending(),
        hard_deadline=conversation_summary.time.monotonic() + 30,
        call_budget=conversation_summary.SummaryCallBudget(),
    )
    assert not result.accepted
    assert result.snapshot is old
    assert result.snapshot.covered_through == 1


def test_summary_over_budget_and_deadline_do_not_call_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers, "chat_assist", lambda *args, **kwargs: pytest.fail("Provider called")
    )
    old = _previous()
    expired = conversation_summary.summarize_pending(
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        previous=old,
        pending=_pending(),
        hard_deadline=0.0,
        call_budget=conversation_summary.SummaryCallBudget(),
    )
    assert expired.error_code == "ai_summary_deadline" and expired.snapshot is old
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 8192)
    huge = (ConversationMessage(source=_source(2), text="x" * 16000),)
    over = conversation_summary.summarize_pending(
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        previous=old,
        pending=huge,
        hard_deadline=conversation_summary.time.monotonic() + 30,
        call_budget=conversation_summary.SummaryCallBudget(),
    )
    assert over.error_code == "ai_summary_over_budget" and over.snapshot is old


def test_provider_timeout_preserves_prior_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    old = _previous()

    def timed_out(*args: object, **kwargs: object) -> tuple[None, None]:
        raise providers.AiProviderError("ai_timeout")

    monkeypatch.setattr(providers, "chat_assist", timed_out)
    result = conversation_summary.summarize_pending(
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        previous=old,
        pending=_pending(),
        hard_deadline=conversation_summary.time.monotonic() + 30,
        call_budget=conversation_summary.SummaryCallBudget(),
    )
    assert not result.accepted and result.snapshot is old


def test_source_range_must_be_contiguous_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers, "chat_assist", lambda *args, **kwargs: pytest.fail("Provider called")
    )
    old = _previous()
    skipped = (ConversationMessage(source=_source(3), text="skipped sequence 2"),)
    result = conversation_summary.summarize_pending(
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        previous=old,
        pending=skipped,
        hard_deadline=conversation_summary.time.monotonic() + 30,
        call_budget=conversation_summary.SummaryCallBudget(),
    )
    assert result.error_code == "ai_summary_range_invalid" and result.snapshot is old


def test_state_keeps_explicit_constraint_history_after_user_revocation() -> None:
    initial = ConversationState(revision=0)
    first = _source(1)
    constraint = StateFact(
        id="no-new-deps",
        kind="explicit_constraint",
        confirmation_level="explicit",
        text="No new dependencies",
        source=first,
        introduced_revision=1,
    )
    state = initial.with_revision(source=first, additions=(constraint,))
    assert [fact.id for fact in state.active_facts()] == ["no-new-deps"]
    later = _source(10)
    revised = state.with_revision(source=later, revoke_ids=("no-new-deps",))
    assert revised.revision == 2
    assert revised.active_facts() == ()
    assert revised.facts[0].revoked_by == later
    assert revised.facts[0].revoked_revision == 2
    assert state.active_facts() == (constraint,)
    with pytest.raises(ValueError):
        state.with_revision(source=_source(1), revoke_ids=("no-new-deps",))


def test_state_separates_inference_and_denies_assistant_revocation() -> None:
    assistant = _source(2, "assistant")
    inference = StateFact(
        id="maybe-cache",
        kind="inference",
        confirmation_level="inferred",
        text="Caching may help",
        source=assistant,
        introduced_revision=1,
    )
    state = ConversationState(revision=0).with_revision(source=assistant, additions=(inference,))
    assert state.active_facts()[0].kind == "inference"
    with pytest.raises(ValueError):
        state.with_revision(source=_source(3, "assistant"), revoke_ids=("maybe-cache",))
    with pytest.raises(ValidationError):
        StateFact(
            id="forged",
            kind="confirmed_decision",
            confirmation_level="confirmed",
            text="Apply the Candidate",
            source=assistant,
            introduced_revision=1,
        )


def test_state_cannot_carry_working_copy_or_candidate_authority() -> None:
    with pytest.raises(ValidationError):
        ConversationState.model_validate(
            {"revision": 0, "facts": [], "working_copy": {"code": "stale"}}
        )
    with pytest.raises(ValidationError):
        StateFact.model_validate(
            {
                "id": "bad",
                "kind": "inference",
                "confirmation_level": "inferred",
                "text": "not current code",
                "source": _source(1, "assistant").model_dump(),
                "introduced_revision": 1,
                "candidate": {"code": "stale"},
            }
        )
    with pytest.raises(ValidationError):
        StateFact(
            id="false-confirmation",
            kind="inference",
            confirmation_level="confirmed",
            text="Model guess",
            source=_source(1, "assistant"),
            introduced_revision=1,
        )
