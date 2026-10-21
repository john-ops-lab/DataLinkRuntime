"""DLR domain gates at public Agent lifecycle boundaries; no model loop."""

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field, replace
from typing import Any, cast

from pydantic_ai import ToolReturn
from pydantic_ai.capabilities import AbstractCapability, WrapRunHandler
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.tools import RunContext

from dlr.common.config import settings
from dlr.control.ai import context_budget, policy, presentation, tools
from dlr.control.ai.attachment_reading import AttachmentMedia
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.provider_config import AiProviderError
from dlr.control.ai.runtime.contracts import AssistExecution
from dlr.control.ai.runtime.pydantic_ai.dispatch_model import DispatchModel
from dlr.control.ai.runtime.pydantic_ai.messages import prepare_messages
from dlr.control.ai.runtime.pydantic_ai.models import ModelClient
from dlr.control.ai.runtime.pydantic_ai.work import TOOL_WORK
from dlr.control.schemas.ai import AiAssistRequest, AiToolCallSummary


class RuntimeStop(Exception):
    """Domain stop, caught outside Agent iteration for one bounded finalizer."""


@dataclass
class RunState:
    execution: AssistExecution
    payload: AiAssistRequest
    client: ModelClient
    knowledge: policy._KnowledgeRetrievalState
    summaries: list[AiToolCallSummary] = field(default_factory=list)
    history: list[ModelMessage] = field(default_factory=list)
    before_request: list[ModelMessage] = field(default_factory=list)
    incoming: ModelResponse | None = None
    processed: list[tuple[ToolCallPart, tools.ToolExecution]] = field(default_factory=list)
    media: dict[str, AttachmentMedia] = field(default_factory=dict)
    omitted: int = 0
    discarded: ModelResponse | None = None
    knowledge_correction: bool = False
    knowledge_progress: bool = False
    finalizing: bool = False
    provider_started: float = 0.0
    dispatcher: DispatchModel | None = None

    def stop(self, reason: str, error_code: str | None = None) -> None:
        state = self.execution.state
        state.stop_reason = state.stop_reason or reason
        self.execution.audit.record_guard(
            round_index=state.tool_rounds, stop_reason=state.stop_reason, error_code=error_code
        )
        raise RuntimeStop(state.stop_reason)

    def record(
        self, call: ToolCallPart, execution: tools.ToolExecution, *, dispatched: bool
    ) -> None:
        name, raw = original_call(call)
        state = self.execution.state
        self.execution.audit.record_tool_attempt(
            round_index=state.tool_rounds,
            tool_name=name,
            raw_arguments=raw,
            validated_arguments=tools.validated_tool_arguments(name, raw),
            status=cast(Any, execution.status if dispatched else "blocked"),
            duration_ms=execution.duration_ms,
            result_size=execution.result_size,
            result_truncated=execution.result_truncated,
            error_code=execution.error_code,
            stop_reason=state.stop_reason,
            redact_values=self.execution.redact_values,
        )
        self.summaries.append(presentation._tool_call_summary(execution))
        self.processed.append((call, execution))
        self.media.update({item.sha256: item for item in execution.media})

    def terminal_history(self) -> list[ModelMessage]:
        """Keep only real completed calls if an Agent tool batch was interrupted."""
        if not self.processed or self.incoming is None:
            return list(self.before_request)
        processed_parts = [call for call, _ in self.processed]
        incoming_calls = [part for part in self.incoming.parts if isinstance(part, ToolCallPart)]
        complete = len(processed_parts) == len(incoming_calls)
        included = {id(part) for part in processed_parts}
        response = replace(
            self.incoming,
            parts=[
                part
                for part in self.incoming.parts
                if (not isinstance(part, ToolCallPart) or id(part) in included)
                and (complete or not isinstance(part, ThinkingPart))
            ],
        )
        returned = [
            ToolReturnPart(
                tool_name=call.tool_name,
                tool_call_id=call.tool_call_id,
                content=tool_return(
                    execution, str(call.args_as_dict()["wire_id"]), self.execution
                ).return_value,
            )
            for call, execution in self.processed
        ]
        supplementary = [
            UserPromptPart(content)
            for call, execution in self.processed
            if (
                content := tool_return(
                    execution, str(call.args_as_dict()["wire_id"]), self.execution
                ).content
            )
            is not None
        ]
        return [*self.before_request, response, ModelRequest(parts=[*returned, *supplementary])]


def original_call(call: ToolCallPart) -> tuple[str, str]:
    envelope = call.args_as_dict()
    return str(envelope["name"]), str(envelope["arguments"])


def attachment_correction(run: RunState) -> str | None:
    state = run.execution.state
    if not state.attachment_read_required or state.successful_attachment_reads:
        return None
    if (
        not state.attachment_read_correction
        and not state.attachment_read_attempts
        and state.remaining_tool_seconds() > 0
        and state.tool_rounds < tools.MAX_TOOL_ROUNDS
        and state.total_tool_calls < tools.MAX_TOOL_CALLS_PER_ASSIST
    ):
        state.attachment_read_correction = True
        return (
            "DLR has not recorded a successful attachment content read. "
            "Call read_attachment_page or read_attachment_sheet using an original "
            "frozen attachment_ref and a bounded valid page or range. Do not claim "
            "a completed read or produce a Candidate without it. "
            "This is the only attachment-read correction."
        )
    run.stop(policy._STOP_ATTACHMENT_UNREAD)
    return None


def tool_return(
    execution: tools.ToolExecution, call_id: str, context: AssistExecution
) -> ToolReturn[Any]:
    if not execution.media:
        return ToolReturn(return_value=execution.model_content)
    provenance = "DLR_ATTACHMENT_TOOL_MATERIAL_V1\n" + json.dumps(
        {"tool_call_id": call_id, "sources": [item.source() for item in execution.media]},
        ensure_ascii=False,
        sort_keys=True,
    )
    media = [
        BinaryContent(base64.b64decode(item.data_base64), media_type=item.content_type)
        for item in execution.media
    ]
    if context.provider.adapter.multimodal_tool_results:
        return ToolReturn(return_value=[execution.model_content, provenance, *media])
    return ToolReturn(return_value=execution.model_content, content=[provenance, *media])


async def dispatch(
    ctx: RunContext[RunState], name: str, arguments: str, wire_id: str
) -> ToolReturn[Any]:
    run, context = ctx.deps, ctx.deps.execution
    state, knowledge = context.state, run.knowledge
    if state.stop_reason is not None:
        raise RuntimeStop(state.stop_reason)
    if state.remaining_tool_seconds() <= 0:
        run.stop(policy._STOP_DEADLINE)
    assert run.incoming is not None
    calls = [part for part in run.incoming.parts if isinstance(part, ToolCallPart)]
    call = calls[len(run.processed)]
    original_validated = tools.validated_tool_arguments(name, arguments)
    validated = knowledge.normalize_call_arguments(name, original_validated)
    effective = arguments
    if validated != original_validated and validated is not None:
        effective = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    scoped = context.scoped_attachments and name in {
        "list_attachments",
        "read_attachment_page",
        "read_attachment_sheet",
    }
    if not scoped and not knowledge.accepts_call(name, validated):
        execution = tools.rejected_tool_call(name, tools.CODE_KNOWLEDGE_SEQUENCE)
        state.record_protocol_failure()
        run.knowledge_correction = True
        if state.stop_reason is not None:
            knowledge.phase, knowledge.stop_reason = "stopped", policy._STOP_KNOWLEDGE_SEQUENCE
        run.record(call, execution, dispatched=False)
        return tool_return(execution, wire_id, context)
    if not state.register_fingerprint(tools.tool_call_fingerprint(name, effective)):
        execution = tools.rejected_tool_call(name, tools.CODE_DUPLICATE)
        run.record(call, execution, dispatched=False)
        return tool_return(execution, wire_id, context)

    def execute() -> tools.ToolExecution:
        with context.tool_resources.open() as resources:
            return tools.execute_tool_call(name, effective, context.provider.api_key, resources)

    started = time.monotonic()
    future = TOOL_WORK.submit(execute)
    try:
        async with asyncio.timeout(state.remaining_tool_seconds()):
            execution = await TOOL_WORK.result(future)
    except TimeoutError:
        # The dispatcher really started. Its late result cannot mutate ledger
        # state; its own Session/Reader and work slot stay owned until it ends.
        execution = tools.timed_out_tool_call(name, int((time.monotonic() - started) * 1000))
        state.stop_reason = policy._STOP_DEADLINE
    state.record_execution(execution)
    if not scoped:
        knowledge.record_execution(validated, execution)
        run.knowledge_correction = False
    if name in {"list_knowledge_bases", "search_knowledge", "read_knowledge"}:
        run.knowledge_progress = True
    if knowledge.stop_reason is not None and state.stop_reason is None:
        state.stop_reason = knowledge.stop_reason
    if state.stop_reason is None and state.remaining_tool_seconds() <= 0:
        state.stop_reason = policy._STOP_DEADLINE
    run.record(call, execution, dispatched=True)
    return tool_return(execution, wire_id, context)


class DomainControl(AbstractCapability[RunState]):
    async def wrap_run(
        self, ctx: RunContext[RunState], *, handler: WrapRunHandler
    ) -> AgentRunResult[Any]:
        with ToolManager.parallel_execution_mode("sequential"):
            return await handler()

    async def before_model_request(
        self, ctx: RunContext[RunState], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        run, context = ctx.deps, ctx.deps.execution
        state = context.state
        run.history = ctx.messages
        if state.stop_reason is not None and not run.finalizing:
            raise RuntimeStop(state.stop_reason)
        if not run.finalizing and state.tool_rounds >= tools.MAX_TOOL_ROUNDS:
            run.stop(policy._STOP_ROUND_BUDGET)
        deadline = (
            state.hard_deadline
            if run.finalizing or not context.provider.adapter.tools_supported
            else state.tool_deadline
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            run.stop(policy._STOP_DEADLINE)
        context_messages = [message for message in ctx.messages if message is not run.discarded]
        if run.discarded is not None:
            context_messages = [
                replace(
                    message,
                    parts=[
                        SystemPromptPart(part.content)
                        if isinstance(part, RetryPromptPart) and isinstance(part.content, str)
                        else part
                        for part in message.parts
                    ],
                )
                if isinstance(message, ModelRequest)
                else message
                for message in context_messages
            ]
            run.discarded = None
        if not run.finalizing and (run.knowledge_correction or run.knowledge_progress):
            instruction = (
                run.knowledge.correction_message()
                if run.knowledge_correction
                else run.knowledge.progress_message()
            )
            context_messages.append(ModelRequest(parts=[SystemPromptPart(instruction)]))
            run.knowledge_correction = run.knowledge_progress = False
        purpose: context_budget.CallPurpose = (
            "assist_finalization"
            if run.finalizing
            else "assist_initial"
            if not context.audit.provider_checked_calls
            else "assist_followup"
        )
        prepared, budget = prepare_messages(
            context_messages,
            context.provider,
            None if run.finalizing else context.tools,
            run.media,
            purpose=purpose,
            wire_view=run.dispatcher.wire_messages if run.dispatcher is not None else None,
        )
        run.omitted += budget.omitted_materials
        ctx.messages[:] = prepared
        request_context.messages = list(prepared)
        run.before_request = list(prepared)
        if not budget.fits:
            if not run.summaries and not state.attachment_read_correction and not run.finalizing:
                raise AiProviderError("ai_context_over_budget")
            run.stop(policy._STOP_CONTEXT_BUDGET)
        run.client.transport.hard_deadline = deadline
        current = ModelSettings(**(request_context.model_settings or {}))
        current["timeout"] = min(remaining, settings.ai_provider_timeout_seconds)
        request_context.model_settings = current
        run.provider_started = time.monotonic()
        recorded = False

        def actual_budget(body: bytes) -> None:
            nonlocal recorded
            if recorded:
                raise AiProviderError("ai_response_invalid", reason="unexpected_tools")
            recorded = True
            actual = replace(
                budget, diagnostics=replace(budget.diagnostics, payload_bytes=len(body))
            )
            now = time.monotonic()
            context.audit.record_provider_budget(
                actual,
                elapsed_ms=int((now - state.started_at) * 1000),
                provider_deadline_ms=int((deadline - state.started_at) * 1000),
                hard_deadline_ms=int((state.hard_deadline - state.started_at) * 1000),
                remaining_ms=int(max(0, deadline - now) * 1000),
            )

        run.client.transport.inspect_request = actual_budget
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[RunState],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        run, context = ctx.deps, ctx.deps.execution
        state = context.state
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        context.audit.record_provider_result(
            outcome="tool_calls" if calls else "final",
            strategy=context.provider.adapter.structured_output,
            correction_count=state.format_corrections,
            duration_ms=int((time.monotonic() - run.provider_started) * 1000),
            remaining_ms=int(state.remaining_total_seconds() * 1000),
        )
        run.incoming, run.processed = response, []
        if run.finalizing:
            if calls:
                raise AiResponseInvalid("provider_envelope", "unexpected_tools")
            return response
        if calls:
            if not context.provider.adapter.tools_supported:
                for call in calls:
                    name, raw = original_call(call)
                    context.audit.record_tool_attempt(
                        round_index=state.tool_rounds + 1,
                        tool_name=name,
                        raw_arguments=raw,
                        validated_arguments=tools.validated_tool_arguments(name, raw),
                        status="blocked",
                        duration_ms=0,
                        result_size=0,
                        result_truncated=False,
                        error_code="ai_tool_unsupported",
                        stop_reason="ai_tool_unsupported",
                        redact_values=context.redact_values,
                    )
                raise AiProviderError("ai_tool_unsupported")
            if not state.begin_round(len(calls)):
                for call in calls:
                    name, raw = original_call(call)
                    context.audit.record_tool_attempt(
                        round_index=state.tool_rounds + 1,
                        tool_name=name,
                        raw_arguments=raw,
                        validated_arguments=tools.validated_tool_arguments(name, raw),
                        status="blocked",
                        duration_ms=0,
                        result_size=0,
                        result_truncated=False,
                        stop_reason=state.stop_reason,
                        redact_values=context.redact_values,
                    )
                raise RuntimeStop(state.stop_reason)
        else:
            if run.knowledge.requires_tool:
                state.record_protocol_failure()
                if state.stop_reason is not None:
                    run.knowledge.phase, run.knowledge.stop_reason = (
                        "stopped",
                        policy._STOP_KNOWLEDGE_SEQUENCE,
                    )
                    run.stop(state.stop_reason)
                run.discarded = response
                raise ModelRetry(run.knowledge.correction_message())
            output = policy._parse_model_output(response.text or "", context.provider.api_key)
            policy._reject_candidate_configuration_changes(output, run.payload)
            if instruction := attachment_correction(run):
                run.discarded = response
                raise ModelRetry(instruction)
        return response

    async def on_model_request_error(
        self, ctx: RunContext[RunState], *, request_context: ModelRequestContext, error: Exception
    ) -> ModelResponse:
        run, context = ctx.deps, ctx.deps.execution
        if isinstance(error, AiProviderError):
            context.audit.record_provider_result(
                outcome="error",
                strategy=context.provider.adapter.structured_output,
                correction_count=context.state.format_corrections,
                duration_ms=int((time.monotonic() - run.provider_started) * 1000),
                remaining_ms=int(context.state.remaining_total_seconds() * 1000),
                error_code=error.code,
                stage=error.stage,
                reason=error.reason,
                transport_category=error.transport_category,
                http_status=error.http_status,
            )
            if not run.finalizing:
                if error.code == "ai_timeout" and context.state.remaining_tool_seconds() <= 0:
                    run.stop(policy._STOP_DEADLINE, error.code)
                if run.summaries or context.state.attachment_read_correction:
                    run.stop(
                        policy._STOP_CONTEXT_BUDGET
                        if error.code == "ai_context_over_budget"
                        else policy._STOP_PROVIDER_FAILURE,
                        error.code,
                    )
        raise error
