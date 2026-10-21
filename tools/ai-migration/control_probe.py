"""Representative D6 bridge only; unknown-tool continuation remains a B0 gap."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, cast

from dlr.control.ai import tools as dlr_tools
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.services.ai import _AssistToolState, _KnowledgeRetrievalState, _parse_model_output
from pydantic_ai import Agent, PromptedOutput, Tool
from pydantic_ai.capabilities import (
    AbstractCapability,
    RawToolArgs,
    WrapRunHandler,
)
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.tools import RunContext, ToolDefinition

from dispatch_model import DISPATCH_TOOL, DispatchModel
from probe_support import FINAL_TEXT, Final

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "string"}},
    "required": ["value"],
    "additionalProperties": False,
}


@dataclass
class Ledger:
    state: _AssistToolState = field(default_factory=lambda: _AssistToolState.create(150))
    validations: list[str] = field(default_factory=list)
    model_calls: int = 0
    dispatch_results: list[dlr_tools.ToolExecution] = field(default_factory=list)
    knowledge: _KnowledgeRetrievalState = field(
        default_factory=lambda: _KnowledgeRetrievalState.create(False, False)
    )
    context: dlr_tools.ToolExecutionContext | None = None
    api_key: str | None = None
    discarded: ModelResponse | None = None


class PolicyStop(Exception):
    pass


class SerialBudget(AbstractCapability[Ledger]):
    async def before_model_request(
        self, ctx: RunContext[Ledger], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        ledger = ctx.deps
        if ledger.state.stop_reason is not None:
            raise PolicyStop(ledger.state.stop_reason)
        if ledger.discarded is not None:

            def clean(messages: list[ModelMessage]) -> list[ModelMessage]:
                return [
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
                    for message in messages
                    if message is not ledger.discarded
                ]

            # Both public views must omit the ungrounded output. The replacement
            # system instruction is fixed domain policy, not framework error text.
            ctx.messages[:] = clean(ctx.messages)
            request_context.messages = clean(request_context.messages)
            ledger.discarded = None
        return request_context

    async def wrap_run(
        self, ctx: RunContext[Ledger], *, handler: WrapRunHandler
    ) -> AgentRunResult[Any]:
        # Public run-wide ordering, including tools not individually marked sequential.
        with ToolManager.parallel_execution_mode("sequential"):
            return await handler()

    async def after_model_request(
        self,
        ctx: RunContext[Ledger],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        if calls and not ctx.deps.state.begin_round(len(calls)):
            raise PolicyStop(ctx.deps.state.stop_reason)
        if not calls:
            ledger = ctx.deps
            if ledger.knowledge.requires_tool:
                ledger.state.record_protocol_failure()
                if ledger.state.stop_reason is not None:
                    raise PolicyStop(ledger.state.stop_reason)
                ledger.discarded = response
                raise ModelRetry(ledger.knowledge.correction_message())
            if not response.text:
                raise AiResponseInvalid("provider_envelope", "empty_content")
            _parse_model_output(response.text, ledger.api_key)
            if (
                ledger.state.attachment_read_required
                and not ledger.state.successful_attachment_reads
            ):
                if (
                    not ledger.state.attachment_read_correction
                    and not ledger.state.attachment_read_attempts
                ):
                    ledger.state.attachment_read_correction = True
                    ledger.discarded = response
                    raise ModelRetry(
                        "DLR has not recorded a successful attachment content read. "
                        "Call read_attachment_page or read_attachment_sheet "
                        "with an original frozen ref. "
                        "This is the only attachment-read correction."
                    )
                ledger.state.stop_reason = "attachment_content_unread"
                raise PolicyStop(ledger.state.stop_reason)
        return response

    async def before_tool_validate(
        self,
        ctx: RunContext[Ledger],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: RawToolArgs,
    ) -> RawToolArgs:
        ctx.deps.validations.append(call.tool_name)
        return args


def bridge(ctx: RunContext[Ledger], **kwargs: Any) -> dict[str, Any]:
    name = ctx.tool_name
    assert name is not None
    return dispatch(ctx.deps, name, json.dumps(kwargs, ensure_ascii=False))


def dispatch(ledger: Ledger, name: str, args: str) -> dict[str, Any]:
    if ledger.state.stop_reason is not None:
        raise PolicyStop(ledger.state.stop_reason)
    original_validated = dlr_tools.validated_tool_arguments(name, args)
    validated = ledger.knowledge.normalize_call_arguments(name, original_validated)
    if validated != original_validated and validated is not None:
        args = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    scoped_attachment = (
        ledger.context is not None
        and ledger.context.attachment_reader is not None
        and name in ("list_attachments", "read_attachment_page", "read_attachment_sheet")
    )
    if not scoped_attachment and not ledger.knowledge.accepts_call(name, validated):
        execution = dlr_tools.rejected_tool_call(name, dlr_tools.CODE_KNOWLEDGE_SEQUENCE)
        ledger.state.record_protocol_failure()
        ledger.dispatch_results.append(execution)
        return cast(dict[str, Any], json.loads(execution.model_content))
    fingerprint = dlr_tools.tool_call_fingerprint(name, args)
    if not ledger.state.register_fingerprint(fingerprint):
        raise PolicyStop(ledger.state.stop_reason)
    execution = dlr_tools.execute_tool_call(name, args, ledger.api_key, ledger.context)
    ledger.state.record_execution(execution)
    if not scoped_attachment:
        ledger.knowledge.record_execution(validated, execution)
    if ledger.knowledge.stop_reason is not None and ledger.state.stop_reason is None:
        ledger.state.stop_reason = ledger.knowledge.stop_reason
    ledger.dispatch_results.append(execution)
    return cast(dict[str, Any], json.loads(execution.model_content))


def agent_for(
    ledger: Ledger,
    *batches: list[ToolCallPart],
    capabilities: list[Any] | None = None,
    compatible: bool = False,
    model_responses: list[ModelResponse] | None = None,
) -> Agent[Ledger, Final]:
    replies = deque(model_responses or [*(ModelResponse(parts=batch) for batch in batches)])

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        ledger.model_calls += 1
        return replies.popleft() if replies else ModelResponse(parts=[TextPart(FINAL_TEXT)])

    tool = Tool.from_schema(
        bridge,
        name="probe_read",
        description="A synthetic read-only tool",
        json_schema=SCHEMA,
        takes_ctx=True,
    )
    scripted_model = FunctionModel(scripted)
    if compatible:
        return compatible_agent(scripted_model, capabilities=capabilities)
    return Agent(
        scripted_model,
        deps_type=Ledger,
        tools=[tool],
        capabilities=[SerialBudget(), *(capabilities or [])],
        output_type=PromptedOutput(Final),
        retries=0,
    )


def compatible_agent(
    model: Model, *, capabilities: list[Any] | None = None
) -> Agent[Ledger, Final]:
    def dispatch_raw(ctx: RunContext[Ledger], name: str, arguments: str) -> dict[str, Any]:
        return dispatch(ctx.deps, name, arguments)

    return Agent(
        DispatchModel(
            model,
            [
                ToolDefinition(
                    name=entry["function"]["name"],
                    description=entry["function"]["description"],
                    parameters_json_schema=entry["function"]["parameters"],
                )
                for entry in dlr_tools.tools_payload(include_knowledge=True)
            ],
        ),
        deps_type=Ledger,
        tools=[Tool(dispatch_raw, name=DISPATCH_TOOL, takes_ctx=True)],
        capabilities=[SerialBudget(), *(capabilities or [])],
        output_type=PromptedOutput(Final),
        # Credit is used only by domain-authorized progress corrections. Strict
        # final/empty-output checks raise stable errors before default retries.
        retries={"tools": 0, "output": 2 * (dlr_tools.MAX_TOOL_ROUNDS + 1) + 1},
        end_strategy="exhaustive",
    )
