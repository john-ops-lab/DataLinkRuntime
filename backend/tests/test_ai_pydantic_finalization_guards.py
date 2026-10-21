"""Budget rejection precedes correction accounting and keeps final stop reasons."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest

from dlr.common.config import settings
from dlr.control.ai import policy, providers, tool_audit, tools
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.provider_config import ProviderSnapshot
from dlr.control.ai.runtime.contracts import AssistExecution
from dlr.control.ai.runtime.pydantic_ai import control
from dlr.control.ai.runtime.pydantic_ai.control import RunState
from dlr.control.ai.runtime.pydantic_ai.messages import initial_messages
from dlr.control.ai.runtime.pydantic_ai.models import model_client
from dlr.control.ai.runtime.pydantic_ai.runtime import correct_format, final_after_stop
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
from dlr.control.ai.runtime.resources import ToolResourceFactory
from dlr.control.schemas.ai import AiAssistRequest, AiSettingDraft, AiToolCallSummary


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
