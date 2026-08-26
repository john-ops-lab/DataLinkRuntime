"""M5.7 Wave C1: controlled read-only Tool Call backend contract tests.

Coverage: 0/1/multiple tool calls; success/failure/timeout/oversized results;
round and total-call budget limits; malformed arguments; unknown /
non-whitelist / write-op tools; Provider-without-capability behavior; DLR
docs list/search/read determinism and bounds; argument/result sanitization;
secret-free logs; final valid/invalid AiModelOutput after tool rounds; and
the pre-C1 backward-compatible no-tool request path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dlr.control.ai import providers
from dlr.control.ai import tools as tools_service
from dlr.control.schemas.ai import AiAssistRequest, AiSettingDraft, AiToolCallSummary
from dlr.control.services import ai as ai_service
from test_ai import (
    assist_body,
    configure,
    create_adapter,
    create_credential,
    fake_chat_response,
    save_version,
    valid_output,
)

PROVIDER_TOKEN = "provider-token-plaintext-sentinel"


def _capture_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        return fake_chat_response(valid_output())

    monkeypatch.setattr(providers, "_request_json", fake_request)
    return captured


def _tool_response(
    tool_calls: list[dict[str, Any]] | None,
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    message_extras: dict[str, object] | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"content": content, "role": "assistant"}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if message_extras:
        message.update(message_extras)
    return {
        "choices": [
            {
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }
        ]
    }


def _final_response(
    message: str = "Generated a candidate.", **candidate_overrides: object
) -> dict[str, object]:
    output = valid_output()
    if candidate_overrides:
        output["candidate"] = {**output["candidate"], **candidate_overrides}
    if message is not None:
        output["message"] = message
    return fake_chat_response(output)


def _call(
    name: str,
    arguments: str = "{}",
    call_id: str = "call-1",
) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


# --- request-local orchestration --------------------------------------------


def test_tool_budget_constants_and_monotonic_deadline_reserve() -> None:
    assert tools_service.MAX_TOOL_ROUNDS == 8
    assert tools_service.MAX_TOOL_CALLS_PER_ASSIST == 16
    state = ai_service._AssistToolState.create(150.0, now=10.0)
    assert state.started_at == 10.0
    assert state.tool_deadline == 130.0
    assert state.hard_deadline == 160.0
    assert state.remaining_tool_seconds(now=25.0) == 105.0
    assert state.remaining_total_seconds(now=25.0) == 135.0


def test_tool_call_fingerprint_normalizes_equivalent_json_and_distinguishes_progress() -> None:
    first = tools_service.tool_call_fingerprint("dlr_docs_search", '{"query":"secrets","limit":2}')
    equivalent = tools_service.tool_call_fingerprint(
        "dlr_docs_search", '{ "limit": 2, "query": "secrets" }'
    )
    progressed = tools_service.tool_call_fingerprint(
        "dlr_docs_search", '{"limit":2,"query":"runtime"}'
    )
    assert first == equivalent
    assert first != progressed
    assert tools_service.tool_call_fingerprint("dlr_runtime_save", "{}") is None

    state = ai_service._AssistToolState.create(150.0, now=0.0)
    assert state.register_fingerprint(first) is True
    assert state.register_fingerprint(equivalent) is False
    assert state.stop_reason == ai_service._STOP_DUPLICATE

    progressed_state = ai_service._AssistToolState.create(150.0, now=0.0)
    assert progressed_state.register_fingerprint(first) is True
    assert progressed_state.register_fingerprint(progressed) is True
    assert progressed_state.stop_reason is None


def test_three_consecutive_tool_failures_stop_and_success_resets_counter() -> None:
    failed = tools_service.execute_tool_call("dlr_runtime_save", '{"code":"x"}', None)
    succeeded = tools_service.execute_tool_call("dlr_docs_list", "{}", None)
    assert failed.status == "error"
    assert succeeded.status == "success"

    reset_state = ai_service._AssistToolState.create(150.0, now=0.0)
    reset_state.record_execution(failed)
    reset_state.record_execution(failed)
    assert reset_state.consecutive_failures == 2
    reset_state.record_execution(succeeded)
    assert reset_state.consecutive_failures == 0
    reset_state.record_execution(failed)
    assert reset_state.consecutive_failures == 1
    assert reset_state.stop_reason is None

    stopped_state = ai_service._AssistToolState.create(150.0, now=0.0)
    for _ in range(3):
        stopped_state.record_execution(failed)
    assert stopped_state.consecutive_failures == 3
    assert stopped_state.total_tool_calls == 3
    assert stopped_state.stop_reason == ai_service._STOP_CONSECUTIVE_FAILURES


def test_expanded_budget_keeps_read_only_whitelist_and_per_call_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert all("save" not in name and "write" not in name for name in tools_service.tool_names())
    unknown = tools_service.execute_tool_call("not_registered", "{}", None)
    write_style = tools_service.execute_tool_call("dlr_runtime_save", '{"code":"x"}', None)
    assert unknown.error_code == tools_service.CODE_UNKNOWN_TOOL
    assert write_style.error_code == tools_service.CODE_UNKNOWN_TOOL

    def slow_handler(_args: dict[str, Any]) -> dict[str, Any]:
        import time

        time.sleep(0.01)
        return {"ok": True}

    monkeypatch.setattr(tools_service, "TOOL_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", slow_handler)
    timed_out = tools_service.execute_tool_call("dlr_docs_list", "{}", None)
    assert timed_out.status == "error"
    assert timed_out.error_code == tools_service.CODE_TIMEOUT


def _finalization_inputs() -> tuple[
    ai_service._AssistToolState,
    AiSettingDraft,
    AiAssistRequest,
    list[AiToolCallSummary],
]:
    state = ai_service._AssistToolState.create(150.0, now=10.0)
    state.stop_reason = ai_service._STOP_CALL_BUDGET
    draft = AiSettingDraft(
        **{
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "model": "manual-model-id",
            "credential_id": None,
            "reasoning_mode": "default",
            "reasoning_effort": None,
        }
    )
    payload = AiAssistRequest.model_validate(assist_body())
    summaries = [AiToolCallSummary(tool_name="dlr_docs_list", status="success", result_size=12)]
    return state, draft, payload, summaries


def test_protection_finalization_disables_tools_and_uses_only_remaining_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, draft, payload, summaries = _finalization_inputs()
    captured: list[dict[str, object]] = []

    def fake_chat_assist(*_: object, **kwargs: object) -> tuple[str, None]:
        captured.append(kwargs)
        return json.dumps(valid_output()), None

    monkeypatch.setattr(ai_service.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(providers, "chat_assist", fake_chat_assist)
    response = ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="en",
        draft=draft,
        api_key=None,
        messages=[],
        image_input=False,
        provider_adapter=providers.get_provider(draft.provider),
        payload=payload,
        executed_tools=summaries,
    )
    assert response.candidate is not None
    assert response.tool_calls == summaries
    assert len(captured) == 1
    assert captured[0]["tools"] is None
    assert captured[0]["timeout_seconds"] == 140.0


@pytest.mark.parametrize("failure", ["tool_call", "timeout", "invalid_json"])
def test_protection_finalization_failures_return_candidate_null_once(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    state, draft, payload, summaries = _finalization_inputs()
    calls = 0

    def fake_chat_assist(
        *_: object, **__: object
    ) -> tuple[str | None, list[providers.NormalizedToolCall] | None]:
        nonlocal calls
        calls += 1
        if failure == "tool_call":
            return None, [providers.NormalizedToolCall("again", "dlr_docs_list", "{}")]
        if failure == "timeout":
            raise providers.AiProviderError("ai_timeout")
        return "not-json", None

    monkeypatch.setattr(ai_service.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(providers, "chat_assist", fake_chat_assist)
    response = ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="zh-CN",
        draft=draft,
        api_key=None,
        messages=[],
        image_input=False,
        provider_adapter=providers.get_provider(draft.provider),
        payload=payload,
        executed_tools=summaries,
    )
    assert calls == 1
    assert response.candidate is None
    assert response.tool_calls == summaries
    assert "工具调用已安全停止" in response.message
    assert "1 个成功结果" in response.message


def test_expired_hard_deadline_returns_english_fallback_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, draft, payload, summaries = _finalization_inputs()
    called = False

    def should_not_call(*_: object, **__: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("expired finalization must not call the Provider")

    monkeypatch.setattr(ai_service.time, "monotonic", lambda: state.hard_deadline)
    monkeypatch.setattr(providers, "chat_assist", should_not_call)
    response = ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="en",
        draft=draft,
        api_key=None,
        messages=[],
        image_input=False,
        provider_adapter=providers.get_provider(draft.provider),
        payload=payload,
        executed_tools=summaries,
    )
    assert called is False
    assert response.candidate is None
    assert "Tool use stopped safely" in response.message
    assert "unconfirmed parts" in response.message


def test_protection_without_success_returns_candidate_null_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, draft, payload, _summaries = _finalization_inputs()
    monkeypatch.setattr(
        providers,
        "chat_assist",
        lambda *_args, **_kwargs: pytest.fail("no evidence must not produce a Candidate"),
    )
    response = ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="zh-CN",
        draft=draft,
        api_key=None,
        messages=[],
        image_input=False,
        provider_adapter=providers.get_provider(draft.provider),
        payload=payload,
        executed_tools=[],
    )
    assert response.candidate is None
    assert "0 个成功结果" in response.message


def test_protection_finalization_rejects_candidate_configuration_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, draft, payload, summaries = _finalization_inputs()
    changed = valid_output()
    assert isinstance(changed["candidate"], dict)
    changed["candidate"]["requirements"] = "provider-must-not-change-this"

    monkeypatch.setattr(ai_service.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(
        providers,
        "chat_assist",
        lambda *_args, **_kwargs: (json.dumps(changed), None),
    )
    response = ai_service._finalize_after_tool_stop(
        state=state,
        system_locale="en",
        draft=draft,
        api_key=None,
        messages=[],
        image_input=False,
        provider_adapter=providers.get_provider(draft.provider),
        payload=payload,
        executed_tools=summaries,
    )
    assert response.candidate is None
    assert response.tool_calls == summaries


# --- Provider capability ----------------------------------------------------


def test_provider_capability_table_is_explicit() -> None:
    assert all(adapter.tools_supported for adapter in providers.PROVIDERS.values())


def test_assist_without_tool_capability_keeps_pre_c1_payload_and_prompt(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider without tool capability gets the byte-identical pre-C1
    protocol: no ``tools`` payload key, no tool prose in the system prompt,
    and a working single-shot assist."""
    adapter = create_adapter(api_client, "no-tool-provider")
    monkeypatch.setitem(
        providers.PROVIDERS,
        "deepseek",
        replace(providers.get_provider("deepseek"), tools_supported=False),
    )
    configure(api_client, provider="deepseek")
    captured: dict[str, object] = {}

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured["payload"] = payload or {}
        return _final_response()

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "tools" not in payload
    assert "tool_choice" not in payload
    system_prompt = payload["messages"][0]["content"]
    assert isinstance(system_prompt, str)
    assert "tool call," in system_prompt  # pre-C1 hard rule kept verbatim
    assert "dlr_docs_list" not in system_prompt
    assert response.json()["tool_calls"] == []


def test_assist_knowledge_retrieval_is_default_off(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool-capable Provider gets only DLR docs until the round opts in."""
    adapter = create_adapter(api_client, "knowledge-default-off")
    configure(api_client)
    captured: dict[str, object] = {}

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured["payload"] = payload or {}
        return _final_response()

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    payload = captured["payload"]
    assert isinstance(payload, dict)
    names = [entry["function"]["name"] for entry in payload["tools"]]
    assert names == ["dlr_docs_list", "dlr_docs_search", "dlr_docs_read"]
    assert "list_knowledge_bases" not in payload["messages"][0]["content"]


def test_assist_with_knowledge_enabled_offers_whitelist_and_rejects_direct_answer(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payload carries the whitelist, but opted-in knowledge retrieval
    cannot be bypassed by a Provider that repeatedly answers directly."""
    adapter = create_adapter(api_client, "tool-capable-zero")
    configure(api_client)
    ima_credential = create_credential(
        api_client,
        name="tool-ima-credential",
        credential_type="access_key",
        fields={"access_key_id": "tool-ima-client", "access_key_secret": "tool-ima-key"},
    )
    configured = api_client.put(
        "/api/knowledge-sources/ima",
        json={"enabled": True, "credential_id": ima_credential["id"]},
    )
    assert configured.status_code == 200, configured.text
    captured: list[dict[str, object]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        return _final_response()

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body()
    body["knowledge_search_enabled"] = True
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    assert len(captured) == 3
    payload = captured[0]
    assert payload["tool_choice"] == "auto"
    tools = payload["tools"]
    assert isinstance(tools, list)
    names = [entry["function"]["name"] for entry in tools]
    # M5.7 Wave C2: the C1 docs whitelist plus the unified read-only
    # KnowledgeSource operations (first target: Tencent ima).
    assert names == [
        "dlr_docs_list",
        "dlr_docs_search",
        "dlr_docs_read",
        "list_knowledge_bases",
        "search_knowledge",
        "read_knowledge",
    ]
    system_prompt = payload["messages"][0]["content"]
    assert isinstance(system_prompt, str)
    assert "tool call," not in system_prompt
    assert "dlr_docs_list" in system_prompt
    body = response.json()
    assert body["candidate"] is None
    assert body["tool_calls"] == []


def test_assist_rejects_fabricated_tool_calls_without_capability(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider without tool capability that fabricates tool calls gets the
    stable actionable ai_tool_unsupported error, never a guessed execution."""
    adapter = create_adapter(api_client, "fabricated-tools")
    monkeypatch.setitem(
        providers.PROVIDERS,
        "kimi",
        replace(providers.get_provider("kimi"), tools_supported=False),
    )
    configure(api_client, provider="kimi")
    calls = 0

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal calls
        calls += 1
        return _tool_response([_call("dlr_docs_list")])

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_tool_unsupported"
    assert calls == 1  # rejected before any execution/retry


# --- 0/1/multiple tool calls ------------------------------------------------


def test_assist_single_tool_call_then_final_answer(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "single-tool")
    configure(api_client)
    captured: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        if len(captured) == 1:
            return _tool_response([_call("dlr_docs_list")])
        return _final_response(message="Answered after one tool call.")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message"] == "Answered after one tool call."
    assert len(body["tool_calls"]) == 1
    summary = body["tool_calls"][0]
    assert summary["tool_name"] == "dlr_docs_list"
    assert summary["status"] == "success"
    assert summary["error_code"] is None
    assert summary["source"] == "dlr-docs:v1:runtime-contract-python"
    assert "dlr-docs" in summary["result_summary"]
    # The tool result really reached the provider on the same chain.
    second_payload = captured[1]
    roles = [m["role"] for m in second_payload["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert second_payload["messages"][2]["tool_calls"][0]["function"]["name"] == "dlr_docs_list"
    assert "dlr-docs:v1" in second_payload["messages"][3]["content"]
    assert "SMOKE_REASONING_MUST_NOT_REACH_BROWSER" not in response.text


def test_assist_multiple_tool_calls_across_rounds(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "multi-tool")
    configure(api_client)
    captured: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        if len(captured) == 1:
            return _tool_response(
                [_call("dlr_docs_list"), _call("dlr_docs_search", '{"query": "secrets"}')]
            )
        if len(captured) == 2:
            return _tool_response([_call("dlr_docs_read", '{"doc_id": "tool-call-contract"}')])
        return _final_response(message="Answered after three calls.")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["message"] == "Answered after three calls."
    assert len(body["tool_calls"]) == 3
    assert [item["tool_name"] for item in body["tool_calls"]] == [
        "dlr_docs_list",
        "dlr_docs_search",
        "dlr_docs_read",
    ]
    assert all(item["status"] == "success" for item in body["tool_calls"])
    assert len(captured) == 3
    # Round 3 message chain: system, user, assistant+tool, assistant+tool.
    assert [m["role"] for m in captured[2]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "tool",
    ]


def test_assist_tool_round_with_attachments_and_snippets_still_valid(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tool calls compose with the Wave B1/B3 context: snippets + attachments
    stay in the request, the final Candidate is still strictly valid."""
    adapter = create_adapter(api_client, "tool-with-context")
    configure(api_client)
    captured: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        if len(captured) == 1:
            return _tool_response([_call("dlr_docs_search", '{"query": "secrets"}')])
        return _final_response(message="context preserved")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body()
    body["context_snippets"] = [
        {"source": "code", "text": "def handle(context, input):", "start_line": 1, "end_line": 1}
    ]
    body["attachments"] = [
        {
            "filename": "notes.txt",
            "content_type": "text/plain",
            "data_base64": "bm90ZS1ib2R5",  # "note-body"
        }
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is not None
    first = captured[0]
    system_prompt = first["messages"][0]["content"]
    assert '"context_snippets"' in system_prompt
    assert '"attachments"' in system_prompt
    assert response.json()["tool_calls"][0]["status"] == "success"


# --- failure / timeout / oversized / malformed / unknown / write ------------


def test_assist_unknown_tool_is_rejected_without_execution(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "unknown-tool")
    configure(api_client)
    monkeypatch.setattr(
        providers,
        "_request_json",
        _tool_then_final([_call("totally_not_registered")]),
    )
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    summary = response.json()["tool_calls"][0]
    assert summary["tool_name"] == "totally_not_registered"
    assert summary["status"] == "error"
    assert summary["error_code"] == "ai_tool_unknown"
    assert "totally_not_registered" in response.text  # name is metadata, safe


def test_assist_write_style_tool_is_rejected(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "write-tool")
    configure(api_client)
    monkeypatch.setattr(
        providers,
        "_request_json",
        _tool_then_final([_call("dlr_runtime_save", '{"code": "x"}')]),
    )
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    summary = response.json()["tool_calls"][0]
    assert summary["status"] == "error"
    assert summary["error_code"] == "ai_tool_unknown"
    # The write intent never reached any handler and never joined the model.
    assert '"code": "x"' not in response.text


def test_assist_malformed_tool_arguments_are_rejected(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "malformed-args")
    configure(api_client)
    for arguments in (
        "not json",
        "[]",
        '{"unknown_key": 1}',
        '{"query": 42}',
        '{"query": "' + "x" * 5000 + '"}',
        '{"limit": true}',
    ):
        monkeypatch.setattr(
            providers,
            "_request_json",
            _malformed_requester(arguments),
        )
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert response.status_code == 200, response.text
        summary = response.json()["tool_calls"][0]
        assert summary["status"] == "error", arguments
        assert summary["error_code"] == "ai_tool_args_invalid", arguments
        # The raw offending input is never echoed back to the browser.
        if arguments.startswith('{"'):
            assert arguments not in response.text
        assert summary["result_summary"] == ""


def _malformed_requester(arguments: str) -> Any:
    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if any(m.get("role") == "tool" for m in payload["messages"]):
            return _final_response()
        return _tool_response([_call("dlr_docs_search", arguments)])

    return fake_request


def _tool_then_final(tool_calls: list[dict[str, Any]], message: str = "Answered.") -> Any:
    """A fake provider that answers tool calls once, then produces the final
    AiModelOutput on the same non-streaming chain (like a well-behaved
    model that stops after seeing the tool results)."""

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if any(m.get("role") == "tool" for m in payload["messages"]):
            return _final_response(message=message)
        return _tool_response(tool_calls)

    return fake_request


def test_assist_tool_timeout_yields_stable_error_result(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "tool-timeout")
    configure(api_client)
    original = tools_service._TOOLS["dlr_docs_list"].handler

    def slow_handler(_args: dict[str, Any]) -> dict[str, Any]:
        import time

        time.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(tools_service, "TOOL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", slow_handler)
    monkeypatch.setattr(
        providers,
        "_request_json",
        _tool_then_final([_call("dlr_docs_list")]),
    )
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", original)
    assert response.status_code == 200, response.text
    summary = response.json()["tool_calls"][0]
    assert summary["status"] == "error"
    assert summary["error_code"] == "ai_tool_timeout"


def test_assist_tool_handler_failure_yields_stable_error_result(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "tool-failure")
    configure(api_client)
    original = tools_service._TOOLS["dlr_docs_list"].handler

    def failing_handler(_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("internal detail must never leak")

    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", failing_handler)
    try:
        monkeypatch.setattr(
            providers,
            "_request_json",
            _tool_then_final([_call("dlr_docs_list")]),
        )
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert response.status_code == 200, response.text
        summary = response.json()["tool_calls"][0]
        assert summary["status"] == "error"
        assert summary["error_code"] == "ai_tool_failed"
        assert "internal detail must never leak" not in response.text
    finally:
        monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", original)


def test_assist_semantically_equivalent_call_is_blocked_without_second_execution(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "duplicate-tool-call")
    configure(api_client)
    original = tools_service._TOOLS["dlr_docs_search"].handler
    handler_calls = 0
    provider_tool_rounds = 0

    def tracked_handler(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return original(args)

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_tool_rounds
        if payload is not None and "tools" not in payload:
            return _final_response(message="Finalized after duplicate protection.")
        provider_tool_rounds += 1
        arguments = (
            '{"query":"secrets","limit":2}'
            if provider_tool_rounds == 1
            else '{ "limit": 2, "query": "secrets" }'
        )
        return _tool_response(
            [_call("dlr_docs_search", arguments, call_id=f"call-{provider_tool_rounds}")]
        )

    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_search"], "handler", tracked_handler)
    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert handler_calls == 1
    assert [summary["status"] for summary in response.json()["tool_calls"]] == [
        "success",
        "error",
    ]
    assert response.json()["tool_calls"][1]["error_code"] == tools_service.CODE_DUPLICATE


def test_assist_stops_after_three_consecutive_tool_failures(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "consecutive-tool-failures")
    configure(api_client)
    provider_tool_rounds = 0

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_tool_rounds
        if payload is not None and "tools" not in payload:
            return _final_response(message="Finalized after three failures.")
        provider_tool_rounds += 1
        return _tool_response(
            [
                _call(
                    f"unknown-write-tool-{provider_tool_rounds}",
                    call_id=f"failure-{provider_tool_rounds}",
                )
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert provider_tool_rounds == 3
    assert response.json()["candidate"] is None
    assert len(response.json()["tool_calls"]) == 3
    assert all(
        summary["error_code"] == tools_service.CODE_UNKNOWN_TOOL
        for summary in response.json()["tool_calls"]
    )


def test_assist_oversized_tool_result_is_truncated_not_echoed(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "oversized-result")
    configure(api_client)
    original = tools_service._TOOLS["dlr_docs_list"].handler

    def huge_handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"blob": "y" * 50_000}

    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", huge_handler)
    try:
        monkeypatch.setattr(
            providers,
            "_request_json",
            _tool_then_final([_call("dlr_docs_list")]),
        )
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert response.status_code == 200, response.text
        summary = response.json()["tool_calls"][0]
        assert summary["status"] == "success"
        assert summary["result_truncated"] is True
        assert len(summary["result_summary"]) <= tools_service.MAX_TOOL_SUMMARY_CHARS + 100
        assert '"y"' * 3 not in response.text  # no huge raw payload in the browser
        assert "y" * 500 not in response.text
    finally:
        monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", original)


def test_truncated_knowledge_read_preserves_provenance() -> None:
    result = {
        "tool": "read_knowledge",
        "item": {
            "id": "browser-success-item",
            "title": "Browser success fixture",
            "content": "safe browser fixture body " * 500,
            "source": "ima:v1:browser-success-item",
        },
    }

    truncated = tools_service._truncate_result(result, ())
    encoded = json.dumps(truncated, ensure_ascii=False, sort_keys=True)

    assert len(encoded) <= tools_service.MAX_TOOL_RESULT_CHARS
    assert truncated["tool"] == "read_knowledge"
    assert truncated["item"]["id"] == "browser-success-item"
    assert truncated["item"]["source"] == "ima:v1:browser-success-item"
    assert truncated["item"]["content"].endswith("…")
    assert truncated["item"]["content"].count("…") == 1
    assert truncated["truncated"] is True


def test_assist_accumulated_result_budget_is_bounded(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "accumulated-budget")
    configure(api_client)
    original = tools_service._TOOLS["dlr_docs_list"].handler

    def fat_handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {"blob": "z" * tools_service.MAX_TOOL_RESULT_CHARS}

    monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", fat_handler)
    monkeypatch.setattr(tools_service, "MAX_TOOL_RESULT_TOTAL_CHARS", 1)
    try:
        monkeypatch.setattr(
            providers,
            "_request_json",
            _tool_then_final([_call("dlr_docs_list")]),
        )
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert response.status_code == 200, response.text
        assert response.json()["candidate"] is not None
        assert len(response.json()["tool_calls"]) == 1
    finally:
        monkeypatch.setattr(tools_service._TOOLS["dlr_docs_list"], "handler", original)


def test_assist_total_call_budget_is_bounded(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "call-budget")
    configure(api_client)
    provider_calls = 0
    tool_rounds = 0

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_calls, tool_rounds
        provider_calls += 1
        if "tools" not in payload:
            return _final_response(message="Finalized from sixteen results.")
        tool_rounds += 1
        return _tool_response(
            [
                _call(
                    "dlr_docs_search",
                    json.dumps({"query": f"query-{tool_rounds}-{index}"}),
                    call_id=f"round-{tool_rounds}-call-{index}",
                )
                for index in range(2)
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Finalized from sixteen results."
    assert len(response.json()["tool_calls"]) == tools_service.MAX_TOOL_CALLS_PER_ASSIST
    assert tool_rounds == tools_service.MAX_TOOL_ROUNDS
    # Eight tool rounds plus one tools-disabled finalization; never a ninth
    # tool round or seventeenth execution.
    assert provider_calls == tools_service.MAX_TOOL_ROUNDS + 1


def test_assist_round_budget_is_bounded_for_large_rounds(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "round-budget")
    configure(api_client)
    provider_calls = 0
    executed = 0
    original_execute = tools_service.execute_tool_call

    def track_execution(*args: object, **kwargs: object) -> tools_service.ToolExecution:
        nonlocal executed
        executed += 1
        return original_execute(*args, **kwargs)  # type: ignore[arg-type]

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_calls
        provider_calls += 1
        if "tools" not in payload:
            return _final_response(message="Stopped before the oversized batch.")
        # One round carrying more calls than the total budget must be
        # rejected before any execution.
        calls = [
            _call("dlr_docs_list", call_id=f"c{i}")
            for i in range(tools_service.MAX_TOOL_CALLS_PER_ASSIST + 1)
        ]
        return _tool_response(calls)

    monkeypatch.setattr(providers, "_request_json", fake_request)
    monkeypatch.setattr(tools_service, "execute_tool_call", track_execution)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is None
    assert response.json()["tool_calls"] == []
    assert executed == 0
    assert provider_calls == 1


def test_assist_round_budget_stops_before_ninth_tool_round(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "eight-round-budget")
    configure(api_client)
    provider_calls = 0
    tool_rounds = 0

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal provider_calls, tool_rounds
        provider_calls += 1
        if "tools" not in payload:
            return _final_response(message="Finalized after eight rounds.")
        tool_rounds += 1
        return _tool_response(
            [
                _call(
                    "dlr_docs_search",
                    json.dumps({"query": f"round-{tool_rounds}"}),
                    call_id=f"round-{tool_rounds}",
                )
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert len(response.json()["tool_calls"]) == tools_service.MAX_TOOL_ROUNDS
    assert tool_rounds == tools_service.MAX_TOOL_ROUNDS
    assert provider_calls == tools_service.MAX_TOOL_ROUNDS + 1


# --- DLR docs tools ---------------------------------------------------------


def test_dlr_docs_list_search_read_deterministic_and_bounded() -> None:
    listing = tools_service.execute_tool_call("dlr_docs_list", '{"category": "runtime"}', None)
    assert listing.status == "success"
    listed = json.loads(listing.model_content)
    assert listed["tool"] == "dlr_docs_list"
    assert listed["total"] == 3
    assert {item["id"] for item in listed["items"]} == {
        "runtime-contract-python",
        "runtime-contract-javascript",
        "runtime-contract-java",
    }
    assert all(item["source"].startswith("dlr-docs:v1:") for item in listed["items"])
    assert listing.source == "dlr-docs:v1:runtime-contract-python"

    repeated = tools_service.execute_tool_call("dlr_docs_list", '{"category": "runtime"}', None)
    assert repeated.model_content == listing.model_content

    search = tools_service.execute_tool_call("dlr_docs_search", '{"query": "secrets"}', None)
    assert search.status == "success"
    searched = json.loads(search.model_content)
    assert searched["total_matches"] >= 1
    assert any(item["id"] == "secrets-and-bindings" for item in searched["items"])
    assert search.source == "dlr-docs:v1:secrets-and-bindings"

    bounded_search = tools_service.execute_tool_call(
        "dlr_docs_search", '{"query": "contract", "limit": 2}', None
    )
    assert bounded_search.status == "success"
    bounded = json.loads(bounded_search.model_content)
    assert bounded["limit"] == 2
    assert len(bounded["items"]) == 2

    read = tools_service.execute_tool_call(
        "dlr_docs_read", '{"doc_id": "runtime-contract-python"}', None
    )
    assert read.status == "success"
    assert read.source == "dlr-docs:v1:runtime-contract-python"
    assert "def handle(context, input)" in read.result_summary
    assert "def handle(context, input)" in read.model_content

    missing = tools_service.execute_tool_call("dlr_docs_read", '{"doc_id": "no-such-doc"}', None)
    assert missing.status == "error"
    assert missing.error_code == "ai_tool_args_invalid"


def test_docs_search_empty_and_case_insensitive() -> None:
    empty = tools_service.execute_tool_call("dlr_docs_search", '{"query": "zzz-no-match"}', None)
    assert empty.status == "success"
    assert json.loads(empty.model_content)["total_matches"] == 0
    upper = tools_service.execute_tool_call("dlr_docs_search", '{"query": "SECRETS"}', None)
    assert upper.status == "success"
    assert json.loads(upper.model_content)["total_matches"] >= 1


def test_tool_args_and_results_sanitize_api_key_and_secret_patterns() -> None:
    execution = tools_service.execute_tool_call(
        "dlr_docs_search",
        json.dumps({"query": f"token {PROVIDER_TOKEN} sk-live-secret-abcdef123456"}),
        PROVIDER_TOKEN,
    )
    assert execution.status == "success"
    # The API key and the common token shape are redacted from the summary.
    assert PROVIDER_TOKEN not in execution.args_summary
    assert "sk-live-secret" not in execution.args_summary
    assert "token [REDACTED] [REDACTED]" in execution.args_summary
    assert PROVIDER_TOKEN not in execution.model_content

    huge = tools_service.execute_tool_call(
        "dlr_docs_search",
        json.dumps({"query": "secrets" * 80}),
        None,
    )
    assert huge.status == "error"
    assert huge.error_code == "ai_tool_args_invalid"
    # The overlong raw input is never echoed verbatim: the summary is
    # sanitized and length-bounded with a deterministic truncation marker.
    assert len(huge.args_summary) <= tools_service.MAX_TOOL_SUMMARY_CHARS + 60
    assert tools_service.truncation_suffix() == "…"
    assert huge.args_summary.endswith(tools_service.truncation_suffix())


def test_tool_logs_contain_only_safe_metadata(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = create_adapter(api_client, "tool-logs")
    configure(api_client)
    monkeypatch.setattr(
        providers,
        "_request_json",
        _tool_then_final([_call("dlr_docs_list"), _call("not_registered")]),
    )
    with caplog.at_level(logging.INFO, logger="dlr.ai.tools"):
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200
    tool_logs = [record.message for record in caplog.records if record.name == "dlr.ai.tools"]
    assert any("status=success" in line and "tool=dlr_docs_list" in line for line in tool_logs)
    assert any(
        "status=error" in line and "code=ai_tool_unknown" in line and "tool=not_registered" in line
        for line in tool_logs
    )
    for line in tool_logs:
        assert "duration_ms=" in line
        assert "size=" in line
        # Never arguments, results, prompts or secrets.
        assert "dlr-docs:v1" not in line
        assert '"items"' not in line
        assert "arguments" not in line


# --- final output contract ---------------------------------------------------


def test_assist_invalid_final_output_after_tools_is_rejected(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "invalid-final")
    configure(api_client)

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            return _tool_response([_call("dlr_docs_list")])
        return _tool_response(None, content="not json at all")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"


def test_assist_candidate_null_after_tools(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "candidate-null-tools")
    configure(api_client)

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            return _tool_response([_call("dlr_docs_read", '{"doc_id": "tool-call-contract"}')])
        return fake_chat_response({"message": "Plain answer, no candidate.", "candidate": None})

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidate"] is None
    assert body["tool_calls"][0]["status"] == "success"
    assert body["tool_calls"][0]["source"] == "dlr-docs:v1:tool-call-contract"


def test_assist_tool_call_does_not_mutate_lifecycle_facts(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "tool-lifecycle")
    version = save_version(api_client, adapter["id"])
    configure(api_client)

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            return _tool_response([_call("dlr_docs_list")])
        return _final_response()

    monkeypatch.setattr(providers, "_request_json", fake_request)
    before = api_client.get(f"/api/adapters/{adapter['id']}").json()
    before_versions = api_client.get(f"/api/adapters/{adapter['id']}/versions").json()
    body = assist_body()
    body["base_version_id"] = version["id"]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200
    after = api_client.get(f"/api/adapters/{adapter['id']}").json()
    after_versions = api_client.get(f"/api/adapters/{adapter['id']}/versions").json()
    assert after["latest_version_id"] == before["latest_version_id"]
    assert after_versions == before_versions


def test_assist_tool_round_keeps_provider_secret_never_reflected(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider API key (resolved from the configured Credential) is
    redacted from tool args/results before they reach the model or browser."""
    credential = api_client.post(
        "/api/credentials",
        json={"name": "tool-provider-key", "type": "token", "fields": {"token": PROVIDER_TOKEN}},
    ).json()
    adapter = create_adapter(api_client, "tool-secret-reflection")
    configure(api_client, provider="openai", credential_id=credential["id"])
    captured: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        if len(captured) == 1:
            return _tool_response(
                [
                    _call(
                        "dlr_docs_search",
                        json.dumps({"query": f"lookup {PROVIDER_TOKEN}"}),
                    )
                ]
            )
        return _final_response()

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert PROVIDER_TOKEN not in response.text
    # The model never received the token either (redacted in the tool result
    # and in the echoed assistant tool-call arguments).
    second = json.dumps(captured[1], ensure_ascii=False)
    assert PROVIDER_TOKEN not in second
