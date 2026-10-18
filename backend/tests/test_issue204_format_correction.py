"""Request-local strict correction without weakening safety or logging output."""

import time

import pytest

from dlr.control.ai import providers
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.schemas.ai import AiSettingDraft
from dlr.control.services import ai


def _draft() -> AiSettingDraft:
    return AiSettingDraft(provider="minimax", model="MiniMax-M3", base_url="https://api.minimax.io")


def _parse(monkeypatch: pytest.MonkeyPatch, first: str, following: str):
    calls: list[dict[str, object]] = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        return following, None

    monkeypatch.setattr(providers, "chat_assist", chat)
    state = ai._AssistToolState.create(30)
    output = ai._parse_with_format_correction(
        first,
        draft=_draft(),
        api_key="private-provider-key",
        messages=[
            {"role": "system", "content": "Output Schema JSON"},
            {"role": "user", "content": "original request"},
        ],
        provider_adapter=providers.get_provider("minimax"),
        state=state,
        audit=None,
    )
    return output, state, calls


@pytest.mark.parametrize("first", ["{broken", '{"message":"x","candidate":null,"extra":1}'])
def test_one_correction_can_succeed(monkeypatch: pytest.MonkeyPatch, first: str) -> None:
    output, state, calls = _parse(monkeypatch, first, '{"message":"fixed","candidate":null}')
    assert output.message == "fixed"
    assert state.format_corrections == 1
    assert len(calls) == 1 and calls[0]["tools"] is None
    assert 0 < calls[0]["timeout_seconds"] <= 30


def test_second_invalid_output_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(providers, "chat_assist", lambda *a, **k: (calls.append(k) or "{bad", None))
    state = ai._AssistToolState.create(30)
    with pytest.raises(AiResponseInvalid) as error:
        ai._parse_with_format_correction(
            "{broken",
            draft=_draft(),
            api_key=None,
            messages=[{"role": "user", "content": "original request"}],
            provider_adapter=providers.get_provider("minimax"),
            state=state,
            audit=None,
        )
    assert error.value.stage == "final_json"
    assert len(calls) == 1 and state.format_corrections == 1


@pytest.mark.parametrize(
    "first,stage",
    [
        ('{"message":"private-provider-key","candidate":null}', "output_safety"),
        ('{"message":"\\ud800","candidate":null}', "unicode"),
    ],
)
def test_safety_failures_never_correct(
    monkeypatch: pytest.MonkeyPatch, first: str, stage: str
) -> None:
    monkeypatch.setattr(
        providers, "chat_assist", lambda *a, **k: pytest.fail("unexpected correction")
    )
    with pytest.raises(AiResponseInvalid) as error:
        ai._parse_with_format_correction(
            first,
            draft=_draft(),
            api_key="private-provider-key",
            messages=[],
            provider_adapter=providers.get_provider("minimax"),
            state=ai._AssistToolState.create(30),
            audit=None,
        )
    assert error.value.stage == stage


@pytest.mark.parametrize("already_corrected,timeout", [(1, 30), (0, 0)])
def test_no_second_correction_or_new_deadline(monkeypatch, already_corrected, timeout) -> None:
    monkeypatch.setattr(
        providers, "chat_assist", lambda *a, **k: pytest.fail("unexpected correction")
    )
    state = ai._AssistToolState.create(timeout, now=time.monotonic())
    state.format_corrections = already_corrected
    with pytest.raises(AiResponseInvalid):
        ai._parse_with_format_correction(
            "{broken",
            draft=_draft(),
            api_key=None,
            messages=[{"role": "user", "content": "original request"}],
            provider_adapter=providers.get_provider("minimax"),
            state=state,
            audit=None,
        )


def test_context_budget_does_not_admit_correction(monkeypatch: pytest.MonkeyPatch) -> None:
    from dlr.common.config import settings

    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 1024)
    monkeypatch.setattr(providers, "chat_assist", lambda *a, **k: pytest.fail("unexpected call"))
    state = ai._AssistToolState.create(30)
    with pytest.raises(AiResponseInvalid):
        ai._parse_with_format_correction(
            "{broken",
            draft=_draft(),
            api_key=None,
            messages=[
                {"role": "system", "content": "Original system contract"},
                {"role": "user", "content": "original request"},
            ],
            provider_adapter=providers.get_provider("minimax"),
            state=state,
            audit=None,
        )
    assert state.format_corrections == 0


def test_correction_transport_failure_is_reported_without_fallback_provider(monkeypatch) -> None:
    from fastapi import HTTPException

    calls = []

    def fail(*args, **kwargs):
        calls.append(args[0].provider)
        raise providers.AiProviderError("ai_provider_unreachable")

    monkeypatch.setattr(providers, "chat_assist", fail)
    with pytest.raises(HTTPException) as error:
        ai._parse_with_format_correction(
            "{broken",
            draft=_draft(),
            api_key=None,
            messages=[{"role": "user", "content": "original request"}],
            provider_adapter=providers.get_provider("minimax"),
            state=ai._AssistToolState.create(30),
            audit=None,
        )
    assert calls == ["minimax"]
    assert error.value.detail["code"] == "ai_provider_unreachable"
