"""One HTTP reply per actual SDK request; no tool execution or model loop."""

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import httpx2
import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.models import ModelRequestParameters

from ai_runtime_support import CapturedRequest, response
from dlr.control.ai import policy, providers, tool_audit
from dlr.control.ai import tools as ai_tools
from dlr.control.ai.provider_config import ProviderSnapshot
from dlr.control.ai.runtime.contracts import AssistExecution
from dlr.control.ai.runtime.pydantic_ai.control import RunState
from dlr.control.ai.runtime.pydantic_ai.messages import initial_messages
from dlr.control.ai.runtime.pydantic_ai.models import model_client, output_type
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
from dlr.control.ai.runtime.resources import ToolResourceFactory
from dlr.control.schemas.ai import AiSettingDraft

ORIGINAL_REQUEST_JSON = providers._request_json


def round_reply(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Test must install one provider reply callback")


ORIGINAL_ROUND_REPLY = round_reply


class FixtureTransport(httpx2.BaseTransport):
    def __init__(self, outer: OwnedTransport) -> None:
        self.hard_deadline = outer.hard_deadline

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        timeout = request.extensions.get("timeout", {}).get("read")
        if providers._request_json is not ORIGINAL_REQUEST_JSON:
            value = providers._request_json(
                request.method,
                str(request.url),
                request.headers,
                payload,
                not_found_code="ai_model_not_found",
                timeout_seconds=timeout,
            )
        else:
            assert round_reply is not ORIGINAL_ROUND_REPLY
            setting = AiSettingDraft(
                provider="custom_openai_compatible",
                base_url="http://fixture.invalid",
                model=payload["model"],
            )
            text, calls = round_reply(
                setting,
                None,
                payload["messages"],
                tools=payload.get("tools"),
                timeout_seconds=timeout,
                hard_deadline=self.hard_deadline,
            )
            message: dict[str, Any] = {"content": text}
            if calls is not None:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ]
            value = {
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if calls else "stop"}
                ]
            }
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx2.ByteStream(json.dumps(value).encode()),
            request=request,
        )


@pytest.fixture(autouse=True)
def sdk_provider_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    original = OwnedTransport.__init__

    def initialize(self: OwnedTransport, **kwargs: Any) -> None:
        original(self, **kwargs)
        if providers._request_json is ORIGINAL_REQUEST_JSON and round_reply is ORIGINAL_ROUND_REPLY:
            return
        self.inner.close()
        self.inner = FixtureTransport(self)  # type: ignore[assignment]

    monkeypatch.setattr(OwnedTransport, "__init__", initialize)


def sdk_request(
    setting: AiSettingDraft,
    messages: list[dict[str, Any]],
    *,
    structured: bool = True,
    adapter: Any = None,
    tools: list[dict[str, Any]] | None = None,
    history: Any = None,
) -> CapturedRequest:
    """Capture one real SDK body, including real Agent schema when requested."""
    adapter = providers.resolve_output_adapter(
        setting,
        providers.resolve_attachment_adapter(
            setting, adapter or providers.get_provider(setting.provider)
        ),
    )
    providers.validate_reasoning(adapter, setting.reasoning_mode, setting.reasoning_effort)
    snapshot = ProviderSnapshot.create(setting, adapter, None)
    captured: list[CapturedRequest] = []
    protocol = {"openai_compatible": "openai", "anthropic": "anthropic", "gemini": "google"}[
        adapter.protocol
    ]

    def request(value: httpx2.Request) -> httpx2.Response:
        captured.append(CapturedRequest(str(value.url), dict(value.headers), value.content))
        body = response(
            protocol,
            text=(
                '{"message":"Contract answer","candidate":null,"request_intent":"consultation"}'
                if structured
                else "OK"
            ),
        ).body
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx2.ByteStream(body),
            request=value,
        )

    original = OwnedTransport.__init__

    def initialize(self: OwnedTransport, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.inner.close()
        self.inner = httpx2.MockTransport(request)  # type: ignore[assignment]

    async def invoke() -> None:
        async with model_client(snapshot, time.monotonic() + 5) as client:
            chain = initial_messages(messages) if history is None else history
            if not chain:
                from pydantic_ai.messages import ModelRequest, UserPromptPart

                chain = [ModelRequest(parts=[UserPromptPart("")])]
            if structured:

                async def unused(**kwargs: Any) -> None:
                    raise AssertionError("No test tool call is expected")

                declared = [
                    Tool.from_schema(
                        unused,
                        name=tool["function"]["name"],
                        description=tool["function"].get("description", ""),
                        json_schema=tool["function"]["parameters"],
                    )
                    for tool in (tools or [])
                ]
                agent = Agent(
                    client.model, output_type=output_type(snapshot), tools=declared, retries=0
                )
                agent.instrument = False
                await agent.run(message_history=chain, model_settings=client.settings)
            else:
                await client.model.request(
                    chain,
                    client.settings,
                    ModelRequestParameters(output_mode="text", function_tools=[], output_tools=[]),
                )

    with patch.object(OwnedTransport, "__init__", initialize):
        asyncio.run(invoke())
    assert len(captured) == 1
    return captured[0]


def sdk_payload(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return sdk_request(*args, **kwargs).json()


def sdk_final_text(provider: Any, value: object) -> str:
    """Run the original raw reply through the production Catalog SDK path."""
    original = OwnedTransport.__init__

    def request(item: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx2.ByteStream(json.dumps(value).encode()),
            request=item,
        )

    def initialize(self: OwnedTransport, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.inner.close()
        self.inner = httpx2.MockTransport(request)  # type: ignore[assignment]

    setting = AiSettingDraft(provider=provider, base_url="https://fixture.invalid", model="fixture")
    with patch.object(OwnedTransport, "__init__", initialize):
        return providers.chat(setting, None, [{"role": "user", "content": "hello"}])


def execution_context(
    *,
    state: Any,
    draft: AiSettingDraft,
    payload: Any,
    messages: Any,
    api_key: str | None = None,
    system_locale: str = "en",
    adapter: Any = None,
    audit: Any = None,
) -> AssistExecution:
    adapter = providers.resolve_output_adapter(
        draft, adapter or providers.get_provider(draft.provider)
    )

    def no_tool_session() -> Any:
        pytest.fail("This final-output fixture must never open a tool Session")

    correlation = tool_audit.AiAuditCorrelation(
        request_id=state.request_id, conversation_id=state.conversation_id
    )
    return AssistExecution(
        provider=ProviderSnapshot.create(draft, adapter, api_key),
        messages=messages,
        tools=None,
        tool_resources=ToolResourceFactory(no_tool_session, ai_tools.ToolExecutionContext()),
        state=state,
        audit=audit or tool_audit.AiToolAuditTrail(correlation=correlation, adapter_id=1),
        system_locale=system_locale,
        knowledge_enabled=False,
        knowledge_available=False,
        scoped_attachments=False,
        has_native_images=False,
    )


@asynccontextmanager
async def run_state(context: AssistExecution, payload: Any):
    async with model_client(context.provider, context.state.hard_deadline) as client:
        run = RunState(
            context, payload, client, policy._KnowledgeRetrievalState.create(False, False)
        )
        run.before_request = initial_messages(context.messages)
        yield run


def finalize(
    *,
    state: Any,
    system_locale: str,
    draft: AiSettingDraft,
    api_key: str | None,
    messages: Any,
    image_input: bool,
    provider_adapter: Any,
    payload: Any,
    executed_tools: Any,
) -> Any:
    """Build actual request resources, then invoke the production finalizer once."""
    from dlr.control.ai.runtime.pydantic_ai.runtime import final_after_stop

    context = execution_context(
        state=state,
        draft=draft,
        payload=payload,
        messages=messages,
        api_key=api_key,
        system_locale=system_locale,
        adapter=provider_adapter,
    )

    async def invoke() -> Any:
        async with run_state(context, payload) as run:
            run.summaries = executed_tools
            return await final_after_stop(run.client.model, run)

    return asyncio.run(invoke())


def correct_final(
    first: str,
    *,
    draft: AiSettingDraft,
    api_key: str | None,
    messages: Any,
    provider_adapter: Any,
    state: Any,
    audit: Any,
) -> Any:
    """Supply an already received raw answer to the real bounded correction."""
    from dlr.control.ai.diagnostics import AiResponseInvalid
    from dlr.control.ai.runtime.pydantic_ai.runtime import correct_format
    from dlr.control.schemas.ai import AiAssistRequest

    first_error = None
    try:
        return policy._parse_model_output(first, api_key)
    except AiResponseInvalid as error:
        first_error = error
    payload = AiAssistRequest.model_validate(
        {"message": "original request", "working_copy": {"code": "", "requirements": ""}}
    )
    context = execution_context(
        state=state,
        draft=draft,
        payload=payload,
        messages=messages,
        api_key=api_key,
        adapter=provider_adapter,
    )

    async def invoke() -> Any:
        async with run_state(context, payload) as run:
            return await correct_format(run.client.model, run, first_error)

    return asyncio.run(invoke())


def request_once(
    draft: AiSettingDraft,
    api_key: str | None,
    messages: Any,
    *,
    state: Any,
    audit: Any,
) -> Any:
    """One real SDK/Agent request with its production budget and audit hooks."""
    from dataclasses import replace

    from dlr.control.ai.runtime.pydantic_ai.runtime import agent
    from dlr.control.schemas.ai import AiAssistRequest

    payload = AiAssistRequest.model_validate(
        {"message": "original request", "working_copy": {"code": "", "requirements": ""}}
    )
    context = execution_context(
        state=state,
        draft=draft,
        payload=payload,
        messages=messages,
        api_key=api_key,
        audit=audit,
        adapter=replace(providers.get_provider(draft.provider), tools_supported=False),
    )

    async def invoke() -> Any:
        async with run_state(context, payload) as run:
            run.finalizing = True
            if not run.before_request:
                from pydantic_ai.messages import ModelRequest, UserPromptPart

                run.before_request = [ModelRequest(parts=[UserPromptPart("")])]
            result = await agent(run.client.model, run, finalizing=True).run(
                None,
                deps=run,
                message_history=run.before_request,
                model_settings=run.client.settings,
            )
            return result.output

    return asyncio.run(invoke())


def stub_preparation(monkeypatch: Any) -> None:
    """Boundary-only tests supply no database/tool execution resources."""
    from types import SimpleNamespace

    from dlr.control.services import ai

    def prepare(session: Any, adapter_id: int, payload: Any, audit: Any, state: Any, **kwargs: Any):
        return SimpleNamespace(state=state)

    monkeypatch.setattr(ai, "_prepare_assist", prepare)


def media_history(setting: AiSettingDraft, content: str, media: Any, *, adapter: Any) -> Any:
    """Use the production ToolReturn bridge, then public paired message parts."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from dlr.control.ai.runtime.pydantic_ai.control import tool_return
    from dlr.control.schemas.ai import AiAssistRequest

    payload = AiAssistRequest.model_validate(
        {"message": "read page", "working_copy": {"code": "", "requirements": ""}}
    )
    state = policy._AssistToolState.create(150)
    context = execution_context(
        state=state, draft=setting, payload=payload, messages=[], adapter=adapter
    )
    execution = ai_tools.ToolExecution(
        tool_name="read_attachment_page",
        status="success",
        args_summary="{}",
        result_summary=content,
        model_content=content,
        error_code=None,
        duration_ms=0,
        result_truncated=False,
        result_size=len(content.encode()),
        source=None,
        media=media,
    )
    returned = tool_return(execution, "call-1", context)
    parts = [ToolReturnPart("read_attachment_page", returned.return_value, tool_call_id="call-1")]
    if returned.content is not None:
        parts.append(UserPromptPart(returned.content))
    return [
        ModelResponse(parts=[ToolCallPart("read_attachment_page", "{}", tool_call_id="call-1")]),
        ModelRequest(parts=parts),
    ]
