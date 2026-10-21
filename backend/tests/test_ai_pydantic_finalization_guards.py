"""Budget rejection precedes correction accounting and keeps final stop reasons."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest

from dlr.common.config import settings
from dlr.control.ai import policy, providers, tool_audit, tools
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.provider_config import ProviderSnapshot
from dlr.control.ai.runtime.contracts import AssistExecution, PreparedAssist
from dlr.control.ai.runtime.pydantic_ai import control
from dlr.control.ai.runtime.pydantic_ai.control import RunState
from dlr.control.ai.runtime.pydantic_ai.messages import initial_messages
from dlr.control.ai.runtime.pydantic_ai.models import model_client
from dlr.control.ai.runtime.pydantic_ai.runtime import (
    PydanticAiRuntime,
    correct_format,
    final_after_stop,
)
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
from dlr.control.ai.runtime.resources import ToolResourceFactory
from dlr.control.schemas.ai import AiAssistRequest, AiSettingDraft, AiToolCallSummary


@pytest.mark.parametrize("runtime", ["legacy", "pydantic_ai"])
def test_format_correction_total_timeout_keeps_provider_error(
    api_client: Any, monkeypatch: Any, runtime: str
) -> None:
    from ai_runtime_support import response, server
    from test_ai import valid_output
    from test_ai_pydantic_runtime import request

    monkeypatch.setattr(settings, "ai_assist_runtime", runtime)
    monkeypatch.setattr(settings, "ai_assist_total_timeout_seconds", 2)
    monkeypatch.setattr(policy, "_FINALIZATION_RESERVE_SECONDS", 0)
    late = response("openai", json.dumps(valid_output()))
    late.wait = threading.Event()
    with server(response("openai", "invalid first response canary"), late) as script:
        result = request(api_client, script)
        assert late.entered.is_set()
        assert result.status_code == 504, result.text
        assert result.json()["detail"]["code"] == "ai_timeout"
        assert len(script.requests) == 2
        assert "tools" not in script.requests[1].json()
        assert b"invalid first response canary" not in script.requests[1].body


@asynccontextmanager
async def _run(state: Any, messages: Any):
    draft = AiSettingDraft(
        provider="custom_openai_compatible", base_url="https://fixture.invalid", model="unlisted"
    )
    payload = AiAssistRequest.model_validate(
        {"message": "original request", "working_copy": {"code": "", "requirements": ""}}
    )

    def no_session() -> Any:
        pytest.fail("Finalization must not execute a tool")

    context = AssistExecution(
        provider=ProviderSnapshot.create(draft, providers.get_provider(draft.provider), None),
        messages=messages,
        tools=None,
        tool_resources=ToolResourceFactory(no_session, tools.ToolExecutionContext()),
        state=state,
        audit=tool_audit.AiToolAuditTrail(
            tool_audit.AiAuditCorrelation(state.request_id, state.conversation_id), adapter_id=1
        ),
        system_locale="en",
        knowledge_enabled=False,
        knowledge_available=False,
        scoped_attachments=False,
        has_native_images=False,
    )
    async with model_client(context.provider, state.hard_deadline) as client:
        run = RunState(
            context, payload, client, policy._KnowledgeRetrievalState.create(False, False)
        )
        run.before_request = initial_messages(messages)
        yield run


@pytest.mark.parametrize("external_cancel", [False, True])
def test_correction_cancelled_before_admission_keeps_original_terminal(
    monkeypatch: Any, external_cancel: bool
) -> None:
    from ai_runtime_support import response

    monkeypatch.setattr(policy, "_FINALIZATION_RESERVE_SECONDS", 0)
    state = policy._AssistToolState.create(2)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(tool_audit, "_emit_record", events.append)
    requests: list[bytes] = []
    entered = asyncio.Event()
    original_hook = control.DomainControl.before_model_request

    async def paused(self: Any, ctx: Any, request_context: Any) -> Any:
        if ctx.deps.pending_format_error is not None:
            entered.set()
            await asyncio.Event().wait()
        return await original_hook(self, ctx, request_context)

    monkeypatch.setattr(control.DomainControl, "before_model_request", paused)
    original_transport = OwnedTransport.__init__

    def reply(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.content)
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx2.ByteStream(response("openai", "invalid original JSON").body),
            request=request,
        )

    def initialize(self: OwnedTransport, **kwargs: Any) -> None:
        original_transport(self, **kwargs)
        self.inner.close()
        self.inner = httpx2.MockTransport(reply)  # type: ignore[assignment]

    monkeypatch.setattr(OwnedTransport, "__init__", initialize)

    async def invoke() -> None:
        async with _run(state, [{"role": "user", "content": "original request"}]) as run:
            task = asyncio.create_task(
                PydanticAiRuntime().run_async(PreparedAssist.create(1, run.payload), run.execution)
            )
            await asyncio.wait_for(entered.wait(), 1)
            if external_cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(AiResponseInvalid) as caught:
                    await task
                assert caught.value.stage == "final_json"
                assert caught.value.reason == "malformed_json"
            assert state.format_corrections == 0

    asyncio.run(invoke())
    assert len(requests) == 1
    assert not [event for event in events if event["event_type"] == "format_correction"]


def test_budget_rejected_correction_does_not_spend_count_or_emit_attempt(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 1024)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(tool_audit, "_emit_record", events.append)
    state = policy._AssistToolState.create(30)
    first_error = AiResponseInvalid("final_json", "malformed_json")

    async def invoke() -> None:
        async with _run(state, [{"role": "user", "content": "original request"}]) as run:
            caught: Exception | None = None
            try:
                await correct_format(run.client.model, run, first_error)
            except Exception as error:
                caught = error
            assert state.format_corrections == 0
            assert caught is first_error
            assert run.pending_format_error is None
            assert run.format_error_call_index == 0
            assert run.execution.audit.provider_checked_calls == 1

    asyncio.run(invoke())
    assert not [event for event in events if event["event_type"] == "format_correction"]
    assert not [event for event in events if event["event_type"] == "provider_result"]
    budgets = [event for event in events if event["event_type"] == "provider_budget"]
    assert len(budgets) == 1
    assert budgets[0]["status"] == "rejected"
    assert budgets[0]["provider_call_index"] == 1
    assert budgets[0]["payload_bytes"] == 0
    assert budgets[0]["estimated_payload_bytes"] > 0


def test_finalizer_budget_rejection_reports_context_without_replacing_original_stop(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 8192)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(tool_audit, "_emit_record", events.append)
    state = policy._AssistToolState.create(150)
    state.stop_reason = policy._STOP_CALL_BUDGET

    async def invoke() -> None:
        async with _run(state, [{"role": "system", "content": "protocol " * 6000}]) as run:
            run.summaries = [AiToolCallSummary(tool_name="dlr_docs_list", status="success")]
            answer = await final_after_stop(run.client.model, run)
            assert answer.candidate is None
            assert "context budget" in answer.message
            assert state.stop_reason == policy._STOP_CALL_BUDGET

    asyncio.run(invoke())
    assert not [event for event in events if event["event_type"] == "provider_result"]


def test_finalizer_timeout_is_recomputed_after_context_preparation(monkeypatch: Any) -> None:
    clock = [110.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 32768)
    monkeypatch.setattr(tool_audit, "_emit_record", lambda event: None)
    prepare = control.prepare_messages

    def prepare_with_cost(*args: Any, **kwargs: Any) -> Any:
        result = prepare(*args, **kwargs)
        clock[0] += 3
        return result

    monkeypatch.setattr(control, "prepare_messages", prepare_with_cost)
    observed: list[float] = []

    def reply(request: httpx2.Request) -> httpx2.Response:
        observed.append(request.extensions["timeout"]["read"])
        body = {
            "choices": [
                {
                    "message": {"content": '{"message":"done","candidate":null}'},
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx2.ByteStream(json.dumps(body).encode()),
            request=request,
        )

    original = OwnedTransport.__init__

    def initialize(self: OwnedTransport, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.inner.close()
        self.inner = httpx2.MockTransport(reply)  # type: ignore[assignment]

    monkeypatch.setattr(OwnedTransport, "__init__", initialize)
    state = policy._AssistToolState.create(150, now=100)
    state.stop_reason = policy._STOP_CALL_BUDGET

    async def invoke() -> None:
        async with _run(state, [{"role": "user", "content": "original request"}]) as run:
            run.summaries = [AiToolCallSummary(tool_name="dlr_docs_list", status="success")]
            answer = await final_after_stop(run.client.model, run)
            assert answer.message == "done"

    asyncio.run(invoke())
    assert observed == [137.0]
