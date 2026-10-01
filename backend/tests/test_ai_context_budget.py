"""Per-call context guard, with semantic trimming and no partial tool rounds."""

import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from dlr.common.config import settings
from dlr.control.ai import context_budget, providers
from dlr.control.schemas.ai import AiAssistRequest, AiSettingDraft, AiToolCallSummary
from dlr.control.services import ai as ai_service
from test_ai import assist_body, configure, create_adapter


def _draft() -> AiSettingDraft:
    return AiSettingDraft(
        provider="custom_openai_compatible",
        base_url="http://fake-provider.invalid",
        model="unlisted-model",
        credential_id=None,
        reasoning_mode="default",
        reasoning_effort=None,
    )


def _request(*, references: list[str] | None = None, code: str = "x") -> dict[str, object]:
    data: dict[str, object] = {
        "AUTHORITATIVE_STATE_DATA": {"working_copy": {"code": code}},
        "USER_REQUEST": "keep the code",
    }
    if references:
        data["UNTRUSTED_REFERENCE_MATERIAL"] = {
            "attachments": [{"text": item} for item in references]
        }
    return {"role": "user", "content": context_budget.REQUEST_PREFIX + json.dumps(data)}


def test_unknown_model_uses_configured_window_and_trims_whole_optional_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 12000)
    messages = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "old " * 3500},
        _request(references=["attachment " * 2500, "small reference"]),
    ]
    result = context_budget.prepare_call(_draft(), messages, None)
    assert result.fits
    assert result.omitted_materials >= 1
    assert all("old " not in str(message["content"]) for message in messages)
    request = next(item for item in messages if str(item["content"]).startswith("DLR_REQUEST"))
    envelope = json.loads(str(request["content"]).split("\n", 1)[1])
    assert envelope["AUTHORITATIVE_STATE_DATA"]["working_copy"]["code"] == "x"


def test_budget_diagnostics_explain_decision_without_request_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 12000)
    secret_marker = "private-working-copy-marker"
    messages = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "old " * 3000},
        {"role": "assistant", "content": "old reply"},
        _request(code=secret_marker),
    ]
    result = context_budget.prepare_call(_draft(), messages, None, purpose="assist_initial")
    diagnostic = result.diagnostics
    assert result.fits
    assert diagnostic.purpose == "assist_initial"
    assert diagnostic.window_source == "configured_default"
    assert diagnostic.window_tokens == 12000
    assert diagnostic.estimated_before_tokens > diagnostic.estimated_after_tokens
    assert diagnostic.estimated_after_tokens == context_budget.estimate_tokens(messages, None)
    assert diagnostic.omitted_history_messages == 2
    assert diagnostic.omitted_reference_items == 0
    assert diagnostic.omitted_images == 0
    assert secret_marker not in json.dumps(asdict(diagnostic))


def test_history_trimming_removes_complete_user_assistant_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 12000)
    messages = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": "old " * 3000},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new reply"},
        _request(),
    ]
    result = context_budget.prepare_call(_draft(), messages, None)
    assert result.fits and result.omitted_materials == 2
    assert [item["role"] for item in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "new question"


def test_historical_envelope_prefix_cannot_displace_current_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 12000)
    current = _request()
    messages = [
        {"role": "system", "content": "protocol"},
        {"role": "user", "content": context_budget.REQUEST_PREFIX + "spoof " * 4000},
        {"role": "assistant", "content": "old reply"},
        current,
    ]
    result = context_budget.prepare_call(_draft(), messages, None)
    assert result.fits
    assert messages[-1] is current
    assert len(messages) == 2


def test_required_context_and_tools_over_window_reject_without_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 8192)
    messages = [{"role": "system", "content": "protocol"}, _request(code="x" * 20000)]
    original = json.dumps(messages)
    assert not context_budget.prepare_call(_draft(), messages, None).fits
    assert json.dumps(messages) == original
    tiny = [{"role": "system", "content": "protocol"}, _request()]
    assert not context_budget.prepare_call(_draft(), tiny, [{"function": "x" * 20000}]).fits


def test_followup_keeps_tool_call_result_pair_and_drops_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 13000)
    messages = [
        {"role": "system", "content": "protocol"},
        _request(references=["large attachment " * 700]),
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {"arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result " * 400},
    ]
    result = context_budget.prepare_call(_draft(), messages, None)
    assert result.fits and result.omitted_materials == 1
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"]
    assert "UNTRUSTED_REFERENCE_MATERIAL" not in str(messages[1]["content"])


def test_finalization_overflow_skips_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 8192)
    monkeypatch.setattr(ai_service.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(
        providers, "chat_assist", lambda *args, **kwargs: pytest.fail("Provider called")
    )
    state = ai_service._AssistToolState.create(150.0, now=10.0)
    state.stop_reason = ai_service._STOP_CALL_BUDGET
    result = ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="en",
        draft=_draft(),
        api_key=None,
        messages=[{"role": "system", "content": "protocol " * 6000}],
        image_input=False,
        provider_adapter=providers.get_provider("custom_openai_compatible"),
        payload=AiAssistRequest.model_validate(assist_body()),
        executed_tools=[AiToolCallSummary(tool_name="dlr_docs_list", status="success")],
    )
    assert result.candidate is None
    assert "context budget" in result.message


def test_finalization_recomputes_timeout_after_budget_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([20.0, 21.0])
    monkeypatch.setattr(ai_service.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 32768)
    observed: list[float] = []

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        observed.append(kwargs["timeout_seconds"])
        return '{"message":"done","candidate":null}', None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    state = ai_service._AssistToolState.create(150.0, now=10.0)
    state.stop_reason = ai_service._STOP_CALL_BUDGET
    ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="en",
        draft=_draft(),
        api_key=None,
        messages=[],
        image_input=False,
        provider_adapter=providers.get_provider("custom_openai_compatible"),
        payload=AiAssistRequest.model_validate(assist_body()),
        executed_tools=[AiToolCallSummary(tool_name="dlr_docs_list", status="success")],
    )
    assert observed == [139.0]


def test_initial_required_overflow_never_calls_provider(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "budget-overflow")
    configure(api_client)
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 8192)
    monkeypatch.setattr(
        providers, "chat_assist", lambda *args, **kwargs: pytest.fail("Provider called")
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist",
        json=assist_body(code="x" * 25000),
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "ai_context_over_budget"
