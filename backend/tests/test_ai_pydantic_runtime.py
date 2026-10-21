"""Production runtime through the API and real SDKs, with synthetic providers."""

import json
import uuid
from typing import Any

import pytest

from ai_runtime_support import Protocol, Reply, response, server
from dlr.common.config import Settings, settings
from dlr.control.ai import tool_audit
from dlr.control.services import ai
from test_ai import assist_body, configure, create_adapter, valid_output


@pytest.fixture(autouse=True)
def pydantic_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_assist_runtime", "pydantic_ai")


def request(
    api_client: Any,
    script: Any,
    protocol: Protocol = "openai",
    body: dict[str, object] | None = None,
) -> Any:
    adapter = create_adapter(api_client, "pydantic-runtime-" + uuid.uuid4().hex[:8])
    configure(
        api_client,
        provider="anthropic"
        if protocol == "anthropic"
        else "gemini"
        if protocol == "google"
        else "custom_openai_compatible",
        base_url=script.url,
    )
    return api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body or assist_body())


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "google"])
def test_api_real_sdk_tool_chain_has_original_wire_tools_and_shared_completion(
    api_client: Any, protocol: Protocol
) -> None:
    with server(
        response(protocol, calls=[("dlr_docs_list", "docs-1", "{}")]), response(protocol)
    ) as script:
        result = request(api_client, script, protocol)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["task_status"] == "answered"
        assert [item["tool_name"] for item in data["tool_calls"]] == ["dlr_docs_list"]
        assert data["tool_calls"][0]["status"] == "success"
        assert len(script.requests) == 2
        assert b"dlr_internal_dispatch" not in script.requests[0].body
        assert b"dlr_internal_dispatch" not in script.requests[1].body


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("unregistered_write", "{}", "ai_tool_unknown"),
        ("dlr_docs_list", '{"category":"a","category":"b"}', "ai_tool_args_invalid"),
        ("dlr_docs_list", "[]", "ai_tool_args_invalid"),
    ],
)
def test_original_bad_tool_arguments_reach_dispatcher_and_count(
    api_client: Any, monkeypatch: pytest.MonkeyPatch, name: str, args: str, code: str
) -> None:
    events: list[dict[str, Any]] = []
    states = []
    prepare = ai._prepare_assist

    def trace(*args: Any, **kwargs: Any) -> Any:
        context = prepare(*args, **kwargs)
        states.append(context.state)
        return context

    monkeypatch.setattr(ai, "_prepare_assist", trace)
    monkeypatch.setattr(tool_audit, "_emit_record", events.append)
    with server(response("openai", calls=[(name, "bad-1", args)]), response("openai")) as script:
        result = request(api_client, script)
        assert result.status_code == 200, result.text
        summary = result.json()["tool_calls"]
        assert len(summary) == 1 and summary[0]["error_code"] == code
        assert len(script.requests) == 2
        assert states[0].total_tool_calls == states[0].tool_rounds == 1
        attempts = [event for event in events if event["event_type"] == "tool_attempt"]
        assert len(attempts) == 1 and attempts[0]["status"] == "error"
        assert events[-1]["failed_calls"] == 1 and events[-1]["blocked_calls"] == 0


def test_entire_over_budget_batch_starts_no_handler(api_client: Any) -> None:
    with server(
        response("openai", calls=[("dlr_docs_list", str(i), "{}") for i in range(17)])
    ) as script:
        result = request(api_client, script)
        assert result.status_code == 200, result.text
        assert result.json()["tool_calls"] == []
        assert result.json()["candidate"] is None
        assert len(script.requests) == 1


def test_duplicate_stops_partial_batch_without_unexecuted_tool_records(api_client: Any) -> None:
    calls = [
        ("dlr_docs_list", "one", "{}"),
        ("dlr_docs_list", "duplicate", "{}"),
        ("dlr_docs_search", "never", '{"query":"not started"}'),
    ]
    with server(response("openai", calls=calls), response("openai")) as script:
        result = request(api_client, script)
        assert result.status_code == 200, result.text
        summaries = result.json()["tool_calls"]
        assert [item["tool_name"] for item in summaries] == ["dlr_docs_list", "dlr_docs_list"]
        assert summaries[0]["status"] == "success"
        assert summaries[1]["error_code"] == "ai_tool_duplicate"
        assert len(script.requests) == 2
        final = script.requests[1].json()
        assert "tools" not in final
        assert "not started" not in script.requests[1].body.decode()
        assistant = [item for item in final["messages"] if item["role"] == "assistant"][-1]
        assert [call["id"] for call in assistant["tool_calls"]] == ["one", "duplicate"]
        assert len([item for item in final["messages"] if item["role"] == "tool"]) == 2


@pytest.mark.parametrize(
    "first", ["invalid first response canary", '{"message":3,"candidate":null}']
)
def test_one_format_correction_disables_tools_and_omits_invalid_text(
    api_client: Any, first: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dlr.control.ai import tool_audit
    from test_issue207_smoke_audit_contract import _contract

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(tool_audit, "_emit_record", events.append)
    with server(response("openai", first), response("openai")) as script:
        result = request(api_client, script)
        assert result.status_code == 200, result.text
        assert len(script.requests) == 2
        assert "tools" not in script.requests[1].json()
        assert first not in script.requests[1].body.decode()
        assert b"only format correction" in script.requests[1].body
        _contract()["validate_correction_audit"](events)
        corrections = [event for event in events if event["event_type"] == "format_correction"]
        assert len(corrections) == 1 and corrections[0]["correction_count"] == 1
        assert corrections[0]["provider_call_index"] == 2
        failures = [event for event in events if event["event_type"] == "response_validation"]
        assert len(failures) == 1 and failures[0]["provider_call_index"] == 1
        results = [event for event in events if event["event_type"] == "provider_result"]
        assert [event["provider_call_index"] for event in results] == [1, 2]


def test_second_invalid_final_fails_without_third_request(api_client: Any) -> None:
    with server(
        response("openai", "first invalid"), response("openai", "second invalid")
    ) as script:
        result = request(api_client, script)
        assert result.status_code == 502, result.text
        assert result.json()["detail"]["code"] == "ai_response_invalid"
        assert len(script.requests) == 2


def test_legacy_configuration_echo_keeps_original_field_presence(api_client: Any) -> None:
    output = valid_output()
    output["request_intent"] = "code_change"
    with server(response("openai", json.dumps(output))) as script:
        result = request(api_client, script)
        assert result.status_code == 200, result.text
        assert result.json()["task_status"] == "candidate_ready"
    output["candidate"]["runtime_config"] = {"forbidden": True}
    with server(response("openai", json.dumps(output))) as script:
        result = request(api_client, script)
        assert result.status_code == 502
        assert len(script.requests) == 1


def test_provider_failure_does_not_fall_back_to_legacy(api_client: Any) -> None:
    with server(Reply(b"upstream secret", 503)) as script:
        result = request(api_client, script)
        assert result.status_code == 502, result.text
        assert result.json()["detail"]["code"] == "ai_provider_http_error"
        assert len(script.requests) == 1


def test_deployment_runtime_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("DLR_AI_RUNTIME", "silently-fallback")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("protocol", ["anthropic", "google"])
def test_native_non_object_arguments_keep_dispatch_budget_and_result(
    api_client: Any, protocol: Protocol, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dlr.control.ai import tools

    states, launched = [], []
    prepare = ai._prepare_assist

    def trace(*args: Any, **kwargs: Any) -> Any:
        context = prepare(*args, **kwargs)
        states.append(context.state)
        return context

    monkeypatch.setattr(ai, "_prepare_assist", trace)
    monkeypatch.setattr(
        tools._TOOLS["dlr_docs_list"], "handler", lambda args: launched.append(args)
    )
    with server(
        response(protocol, calls=[("dlr_docs_list", "bad-native", "[]")]), response(protocol)
    ) as script:
        result = request(api_client, script, protocol)
        assert result.status_code == 200, result.text
        summaries = result.json()["tool_calls"]
        assert len(summaries) == 1
        assert summaries[0]["error_code"] == "ai_tool_args_invalid"
        assert len(script.requests) == 2
        assert not launched
        assert states[0].total_tool_calls == states[0].tool_rounds == 1


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "google"])
def test_text_with_tool_calls_cannot_complete_before_followup(
    api_client: Any, protocol: Protocol
) -> None:
    first = response(protocol, calls=[("dlr_docs_list", "text-and-tool", "{}")])
    raw = json.loads(first.body)
    tempting = json.dumps(
        {"message": "This answer must not win", "candidate": None, "request_intent": "consultation"}
    )
    if protocol == "openai":
        raw["choices"][0]["message"]["content"] = tempting
    elif protocol == "anthropic":
        raw["content"].insert(0, {"type": "text", "text": tempting})
    else:
        raw["candidates"][0]["content"]["parts"].insert(0, {"text": tempting})
    first.body = json.dumps(raw).encode()
    with server(first, response(protocol)) as script:
        result = request(api_client, script, protocol)
        assert result.status_code == 200, result.text
        assert result.json()["message"] == "Contract answer"
        assert len(script.requests) == 2
        assert len(result.json()["tool_calls"]) == 1


@pytest.mark.parametrize("protocol", ["anthropic", "google"])
def test_rejected_long_native_arguments_keep_valid_followup_and_real_budget(
    api_client: Any, monkeypatch: pytest.MonkeyPatch, protocol: Protocol
) -> None:
    from dataclasses import replace

    from dlr.control.ai import tools

    states = []
    launched = []
    prepare = ai._prepare_assist

    def trace(*args: Any, **kwargs: Any) -> Any:
        context = prepare(*args, **kwargs)
        states.append(context.state)
        return context

    def never(arguments: Any) -> Any:
        launched.append(arguments)
        return {}

    monkeypatch.setattr(ai, "_prepare_assist", trace)
    monkeypatch.setitem(
        tools._TOOLS, "dlr_docs_search", replace(tools._TOOLS["dlr_docs_search"], handler=never)
    )
    raw = json.dumps({"query": "x" * 5000})
    with server(
        response(protocol, calls=[("dlr_docs_search", "long-native", raw)]), response(protocol)
    ) as script:
        result = request(api_client, script, protocol)
        assert result.status_code == 200, result.text
        assert result.json()["message"] == "Contract answer"
        summaries = result.json()["tool_calls"]
        assert len(summaries) == 1
        assert summaries[0]["error_code"] == "ai_tool_args_invalid"
        assert states[0].total_tool_calls == states[0].tool_rounds == 1
        assert not launched
        assert len(script.requests) == 2
        body = script.requests[1].json()
        if protocol == "anthropic":
            echoed = next(
                part
                for message in body["messages"]
                for part in message["content"]
                if isinstance(part, dict) and part.get("type") == "tool_use"
            )
            assert echoed["input"] == {}
        else:
            echoed = next(
                part["functionCall"]
                for message in body["contents"]
                for part in message["parts"]
                if "functionCall" in part
            )
            assert echoed["args"] == {}
