"""Independent SDK byte oracles and private continuation accounting."""

import asyncio
import base64
import json
import time
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from ai_runtime_support import Protocol, response, server
from dlr.common.config import settings
from dlr.control.ai import context_budget, tool_audit
from dlr.control.ai.runtime.pydantic_ai.messages import budget_view
from dlr.control.ai.runtime.pydantic_ai.models import model_client, output_type
from test_ai_sdk_contract import snapshot
from test_issue202_model_reading import media

CANARY = "continuation-private-canary"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_contract",
            "description": "Read a synthetic contract.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
]


@pytest.mark.parametrize("protocol", ("openai", "anthropic", "google"))
@pytest.mark.parametrize("mode", ("prompt_only", "json_object", "json_schema"))
def test_conservative_estimate_covers_real_sdk_text_schema_and_media_bytes(
    protocol: Protocol,
    mode: str,
) -> None:
    image = media()
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    [
                        '中文、引号"、反斜线\\与换行\n' * 100,
                        BinaryContent(
                            base64.b64decode(image.data_base64), media_type=image.content_type
                        ),
                    ]
                )
            ]
        )
    ]

    async def invoke(value):
        async with model_client(value, time.monotonic() + 3) as client:
            agent = Agent(client.model, output_type=output_type(value), retries=0)
            agent.instrument = False
            await agent.run(message_history=history, model_settings=client.settings)

    with server(response(protocol)) as script:
        value = snapshot(protocol, script.url, mode)
        view, _ = budget_view(history, {image.sha256: image})
        estimate = context_budget.estimated_payload_bytes(value.draft(), view, None)
        asyncio.run(invoke(value))
        assert len(script.requests) == 1
        # This is the SDK's original body, never a reserialized parsed dict.
        assert estimate >= len(script.requests[0].body)
        assert image.data_base64.encode() in script.requests[0].body


@pytest.mark.parametrize(
    "kind", ("google_tool", "google_text", "google_thinking", "openai_extra", "anthropic_redacted")
)
def test_private_parts_grow_both_budgets_echo_exactly_and_never_enter_diagnostics(
    kind,
    monkeypatch,
) -> None:
    protocol: Protocol = (
        "google"
        if kind.startswith("google")
        else "openai"
        if kind == "openai_extra"
        else "anthropic"
    )
    signature = base64.b64encode((CANARY * 500).encode()).decode()
    extra = {"vendor": {"value": CANARY * 500, "quoted": '中文"\\'}}
    call = ToolCallPart("read_contract", "{}", "contract-call", provider_name=protocol)
    parts = [call]
    if kind == "google_tool":
        call.provider_details = {"thought_signature": signature}
    elif kind == "google_text":
        parts = [TextPart("Reading", provider_details={"thought_signature": signature}), call]
    elif kind == "google_thinking":
        parts = [
            ThinkingPart(
                "private reasoning",
                provider_name="google",
                signature=signature,
                provider_details={"thought_signature": signature},
            ),
            call,
        ]
    elif kind == "openai_extra":
        call.provider_details = {"extra_content": extra}
    else:
        parts = [
            ThinkingPart(
                "", id="redacted_thinking", signature=signature, provider_name="anthropic"
            ),
            call,
        ]
    history = [
        ModelRequest(parts=[UserPromptPart("Read, then answer.")]),
        ModelResponse(parts=parts, provider_name=protocol),
        ModelRequest(parts=[ToolReturnPart("read_contract", "safe result", "contract-call")]),
    ]
    original = deepcopy(history)
    view, _ = budget_view(history, {})
    row = view[1]
    assert (
        row["private_utf8_bytes"] > 10_000
        and row["private_ascii_bytes"] >= row["private_utf8_bytes"]
    )
    assert row["response_part_count"] == len(parts)
    without_private = [
        {key: value for key, value in item.items() if not key.startswith("private_")}
        for item in view
    ]
    token_delta = context_budget.estimate_tokens(view, None) - context_budget.estimate_tokens(
        without_private, None
    )
    assert token_delta >= row["private_utf8_bytes"] // 2
    assert signature not in json.dumps(view) and CANARY not in json.dumps(view)
    assert history == original
    emitted = []
    monkeypatch.setattr(tool_audit, "_emit_record", emitted.append)

    async def invoke(value):
        async with model_client(value, time.monotonic() + 3) as client:
            await client.model.request(
                history,
                client.settings,
                ModelRequestParameters(
                    output_mode="text",
                    output_tools=[],
                    function_tools=[
                        ToolDefinition(
                            name="read_contract",
                            parameters_json_schema=TOOLS[0]["function"]["parameters"],
                        )
                    ],
                ),
            )

    with server(response(protocol)) as script:
        value = snapshot(protocol, script.url)
        budget = context_budget.prepare_call(value.draft(), view, TOOLS, adapter=value.adapter)
        assert budget.fits
        assert budget.diagnostics.payload_bytes == 0
        tool_audit.AiToolAuditTrail(
            tool_audit.new_request_correlation(None), 1
        ).record_provider_budget(
            budget,
            elapsed_ms=0,
            provider_deadline_ms=1000,
            hard_deadline_ms=1000,
            remaining_ms=1000,
        )
        assert signature not in json.dumps(asdict(budget.diagnostics))
        assert CANARY not in json.dumps(emitted)
        asyncio.run(invoke(value))
        assert len(script.requests) == 1
        actual = script.requests[0]
        assert budget.diagnostics.estimated_payload_bytes >= len(actual.body)
        wire = actual.json()
        if kind == "openai_extra":
            assert wire["messages"][1]["tool_calls"][0]["extra_content"] == extra
        elif kind == "anthropic_redacted":
            assert wire["messages"][1]["content"][0] == {
                "type": "redacted_thinking",
                "data": signature,
            }
        else:
            signatures = [
                part.get("thoughtSignature", part.get("thought_signature"))
                for message in wire["contents"]
                for part in message["parts"]
                if "thoughtSignature" in part or "thought_signature" in part
            ]
            assert any(
                base64.urlsafe_b64decode(found) == base64.b64decode(signature)
                for found in signatures
            )
        assert history == original


def test_repeated_private_signatures_and_media_are_counted_per_occurrence():
    signature = base64.b64encode(CANARY.encode() * 100).decode()
    part = ToolCallPart(
        "read_contract",
        "{}",
        "one",
        provider_name="google",
        provider_details={"thought_signature": signature},
    )
    single, _ = budget_view([ModelResponse(parts=[part], provider_name="google")], {})
    twice, _ = budget_view([ModelResponse(parts=[part, part], provider_name="google")], {})
    assert twice[0]["private_utf8_bytes"] == 2 * single[0]["private_utf8_bytes"]
    assert twice[0]["private_ascii_bytes"] == 2 * single[0]["private_ascii_bytes"]
    image = media()
    one = [{"role": "user", "content": [{"type": "dlr_media", "media": image}]}]
    two = deepcopy(one)
    two[0]["content"].append({"type": "dlr_media", "media": image})
    value = snapshot("openai", "https://fixture.invalid")
    assert context_budget.estimated_payload_bytes(
        value.draft(), two, None
    ) - context_budget.estimated_payload_bytes(value.draft(), one, None) >= len(image.data_base64)


def test_byte_limit_prunes_a_whole_reference_before_rejecting_required_context(monkeypatch):
    value = snapshot("google", "https://fixture.invalid")
    data = {
        "USER_REQUEST": "Keep the complete working copy.",
        "AUTHORITATIVE_STATE_DATA": {"working_copy": {"code": "complete-code"}},
        "UNTRUSTED_REFERENCE_MATERIAL": {"context_snippets": [{"text": "中文" * 4000}]},
    }
    messages = [{"role": "user", "content": context_budget.REQUEST_PREFIX + json.dumps(data)}]
    required = deepcopy(data)
    del required["UNTRUSTED_REFERENCE_MATERIAL"]
    required_messages = [
        {
            "role": "user",
            "content": context_budget.REQUEST_PREFIX
            + json.dumps(required, ensure_ascii=False, sort_keys=True),
        }
    ]
    threshold = context_budget.estimated_payload_bytes(value.draft(), required_messages, None) + 1
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 1_000_000)
    result = context_budget.prepare_call(
        value.draft(), messages, None, adapter=replace(value.adapter, max_request_bytes=threshold)
    )
    assert result.fits and result.diagnostics.omitted_reference_items == 1
    retained = json.loads(messages[0]["content"].split("\n", 1)[1])
    assert retained == required
    rejected = context_budget.prepare_call(
        value.draft(),
        messages,
        None,
        adapter=replace(value.adapter, max_request_bytes=threshold - 2),
    )
    assert not rejected.fits
    assert json.loads(messages[0]["content"].split("\n", 1)[1]) == required


def test_nonstring_tool_return_estimate_covers_actual_nested_json_escaping():
    content = {"items": [{"value": '中文"\\\n' * 500, "nested": {"answer": 13}}]}
    history = [
        ModelRequest(parts=[UserPromptPart("Read and answer.")]),
        ModelResponse(parts=[ToolCallPart("read_contract", "{}", "contract-call")]),
        ModelRequest(parts=[ToolReturnPart("read_contract", content, "contract-call")]),
    ]

    async def invoke(value):
        async with model_client(value, time.monotonic() + 3) as client:
            await client.model.request(
                history,
                client.settings,
                ModelRequestParameters(output_mode="text", function_tools=[], output_tools=[]),
            )

    with server(response("openai")) as script:
        value = snapshot("openai", script.url)
        view, _ = budget_view(history, {})
        estimate = context_budget.estimated_payload_bytes(value.draft(), view, None)
        asyncio.run(invoke(value))
        assert len(script.requests) == 1
        assert estimate >= len(script.requests[0].body)
        assert json.loads(script.requests[0].json()["messages"][2]["content"]) == content
