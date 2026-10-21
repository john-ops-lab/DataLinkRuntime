"""Representative D6 bridge only; unknown-tool continuation remains a B0 gap."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

from dlr.control.ai import tools as dlr_tools
from dlr.control.services.ai import _AssistToolState
from pydantic_ai import Agent, PromptedOutput, Tool
from pydantic_ai.capabilities import (
    AbstractCapability,
    RawToolArgs,
    WrapRunHandler,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tool_manager import ToolManager
from pydantic_ai.tools import RunContext, ToolDefinition

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


class PolicyStop(Exception):
    pass


class SerialBudget(AbstractCapability[Ledger]):
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
    if ctx.deps.state.stop_reason is not None:
        raise PolicyStop(ctx.deps.state.stop_reason)
    name = ctx.tool_name
    assert name is not None
    args = json.dumps(kwargs, ensure_ascii=False)
    fingerprint = dlr_tools.tool_call_fingerprint(name, args)
    if not ctx.deps.state.register_fingerprint(fingerprint):
        raise PolicyStop(ctx.deps.state.stop_reason)
    execution = dlr_tools.execute_tool_call(name, args, None)
    ctx.deps.state.record_execution(execution)
    ctx.deps.dispatch_results.append(execution)
    return cast(dict[str, Any], json.loads(execution.model_content))


def agent_for(
    ledger: Ledger, *batches: list[ToolCallPart], capabilities: list[Any] | None = None
) -> Agent[Ledger, Final]:
    replies = deque([*(ModelResponse(parts=batch) for batch in batches)])

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
    return Agent(
        FunctionModel(scripted),
        deps_type=Ledger,
        tools=[tool],
        capabilities=[SerialBudget(), *(capabilities or [])],
        output_type=PromptedOutput(Final),
        retries=0,
    )
