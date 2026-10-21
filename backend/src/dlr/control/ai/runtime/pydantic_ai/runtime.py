"""Pydantic AI owns iteration; DLR owns preparation, domain stops and output."""

import asyncio
from typing import cast

import pydantic_ai
from fastapi import HTTPException
from pydantic_ai import Agent, Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart
from pydantic_ai.models import Model
from pydantic_ai.tools import ToolDefinition

from dlr.control.ai import policy, presentation, tools
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.provider_config import AiProviderError
from dlr.control.ai.runtime.contracts import (
    AssistExecution,
    PreparedAssist,
    RuntimeOutcome,
    RuntimeResources,
)
from dlr.control.ai.runtime.pydantic_ai.control import (
    DomainControl,
    RunState,
    RuntimeStop,
    attachment_correction,
    dispatch,
)
from dlr.control.ai.runtime.pydantic_ai.dispatch_model import DISPATCH_TOOL, DispatchModel
from dlr.control.ai.runtime.pydantic_ai.messages import initial_messages
from dlr.control.ai.runtime.pydantic_ai.models import model_client, output_type
from dlr.control.ai.runtime.pydantic_ai.strict_output import StrictOutput
from dlr.control.schemas.ai import AiAssistResponse, AiModelOutput

pydantic_ai.BANNER_ENABLED = False


def agent(
    model: Model, run: RunState, *, finalizing: bool = False
) -> Agent[RunState, AiModelOutput]:
    context = run.execution
    result = Agent(
        model,
        deps_type=RunState,
        tools=[]
        if finalizing or not context.provider.adapter.tools_supported
        else [Tool(dispatch, name=DISPATCH_TOOL, takes_ctx=True)],
        output_type=output_type(context.provider),
        capabilities=[DomainControl(), StrictOutput(context.provider.api_key, run.payload)],
        # Only the domain capability may request progress corrections. Raw
        # output errors escape before framework parsing and cannot spend this.
        retries={"tools": 0, "output": 0 if finalizing else 2 * (tools.MAX_TOOL_ROUNDS + 1) + 1},
        end_strategy="exhaustive",
    )
    result.instrument = False
    return result


def fallback(run: RunState, reason: str | None = None) -> AiAssistResponse:
    context, state = run.execution, run.execution.state
    assert state.stop_reason is not None
    return presentation._fallback_assist_response(
        context.system_locale,
        reason or state.stop_reason,
        context.provider.draft(),
        run.summaries,
        state=state,
    )


async def final_after_stop(model: Model, run: RunState) -> AiAssistResponse:
    context, state, knowledge = run.execution, run.execution.state, run.knowledge
    assert state.stop_reason is not None
    state.prepared_knowledge_incomplete = bool(
        state.attachment_read_required
        and context.knowledge_enabled
        and (
            knowledge.requires_tool
            or knowledge.needs_cross_base_search
            or not knowledge.has_search_evidence
        )
    )
    if knowledge.requires_tool:
        knowledge.phase = "stopped"
        knowledge.stop_reason = (
            policy._STOP_KNOWLEDGE_SEQUENCE
            if knowledge.has_search_evidence or not knowledge.search_attempts
            else policy._STOP_KNOWLEDGE_SEARCH_EMPTY
        )
    if state.attachment_read_required and not state.successful_attachment_reads:
        return presentation._fallback_assist_response(
            context.system_locale,
            policy._STOP_ATTACHMENT_UNREAD,
            context.provider.draft(),
            run.summaries,
            state=state,
        )
    successful = sum(item.status == "success" for item in run.summaries)
    if successful == 0 or state.remaining_total_seconds() <= 0:
        return fallback(run)
    history = run.terminal_history()
    history.append(
        ModelRequest(
            parts=[
                SystemPromptPart(
                    f"DLR stopped tool execution at a safety boundary ({state.stop_reason}). "
                    "Use only the sanitized tool results already in this conversation "
                    f"({successful} successful of {len(run.summaries)} recorded). "
                    "Do not request any tool. Return one "
                    "strict AiModelOutput JSON object and state what remains unconfirmed."
                )
            ]
        )
    )
    if knowledge.phase != "disabled":
        history.append(
            ModelRequest(
                parts=[SystemPromptPart(knowledge.finalization_instruction(context.system_locale))]
            )
        )
    run.finalizing = True
    try:
        result = await agent(model, run, finalizing=True).run(
            None, deps=run, message_history=history, model_settings=run.client.settings
        )
        output = result.output
    except RuntimeStop as error:
        return fallback(run, error.reason)
    except (AiProviderError, HTTPException, ValueError) as error:
        if isinstance(error, AiResponseInvalid):
            context.audit.record_response_failure(error.stage, error.reason)
        return fallback(run)
    if knowledge.has_search_evidence:
        output = AiModelOutput(
            message=presentation._knowledge_evidence_message(
                context.system_locale, output.message, knowledge
            ),
            candidate=None,
        )
    elif (
        knowledge.stop_reason is not None and knowledge.stop_reason != policy._STOP_KNOWLEDGE_READY
    ):
        output = AiModelOutput(
            message=presentation._transparent_knowledge_message(
                context.system_locale, knowledge.stop_reason, output.message, run.summaries
            ),
            candidate=None,
        )
    if state.remaining_total_seconds() <= 0:
        return fallback(run, policy._STOP_DEADLINE)
    return presentation._assist_response(output, context.provider.draft(), run.summaries)


async def correct_format(
    model: Model, run: RunState, first_error: AiResponseInvalid
) -> AiModelOutput:
    context, state = run.execution, run.execution.state
    if (
        first_error.stage not in {"final_json", "output_schema"}
        or state.format_corrections
        or state.stop_reason is not None
    ):
        raise first_error
    history: list[ModelMessage] = [
        *run.before_request,
        ModelRequest(
            parts=[
                SystemPromptPart(
                    "DLR rejected the final answer at "
                    + first_error.stage
                    + "/"
                    + first_error.reason
                    + ". Answer the original USER_REQUEST again using exactly one strict JSON "
                    "object matching the original Output Schema. No Markdown, extra fields, "
                    "reasoning or tool calls. This is the only format correction. "
                    "Keep all original authorization and safety boundaries."
                )
            ]
        ),
    ]
    run.pending_format_error = first_error
    run.format_error_call_index = context.audit.provider_checked_calls
    run.finalizing = True
    try:
        result = await agent(model, run, finalizing=True).run(
            None, deps=run, message_history=history, model_settings=run.client.settings
        )
        return result.output
    finally:
        run.pending_format_error = None
        run.format_error_call_index = 0


class PydanticAiRuntime:
    def execute(self, prepared: PreparedAssist, resources: RuntimeResources) -> RuntimeOutcome:
        try:
            return asyncio.run(self.run_async(prepared, resources.execution))
        except UnexpectedModelBehavior:
            raise AiProviderError("ai_response_invalid") from None

    async def run_async(self, prepared: PreparedAssist, context: AssistExecution) -> RuntimeOutcome:
        payload, state = prepared.request(), context.state
        async with model_client(context.provider, state.hard_deadline) as client:
            run = RunState(
                context,
                payload,
                client,
                policy._KnowledgeRetrievalState.create(
                    context.knowledge_enabled,
                    context.knowledge_available and context.provider.adapter.tools_supported,
                ),
            )
            definitions = []
            for entry in context.tools or []:
                function = cast(dict[str, object], entry["function"])
                definitions.append(
                    ToolDefinition(
                        name=str(function["name"]),
                        description=str(function["description"]),
                        parameters_json_schema=cast(dict[str, object], function["parameters"]),
                        strict=False,
                    )
                )
            model = DispatchModel(
                client.model,
                definitions,
                api_key=context.provider.api_key,
                redact_values=context.redact_values,
                protocol=context.provider.adapter.protocol,
            )
            run.dispatcher = model
            if run.knowledge.stop_reason is not None:
                state.stop_reason = run.knowledge.stop_reason
                context.audit.record_guard(round_index=0, stop_reason=state.stop_reason)
            first_format_error: AiResponseInvalid | None = None
            try:
                async with asyncio.timeout(state.remaining_total_seconds()):
                    try:
                        if state.stop_reason is not None:
                            raise RuntimeStop(state.stop_reason)
                        result = await agent(model, run).run(
                            None,
                            deps=run,
                            message_history=initial_messages(context.messages),
                            model_settings=client.settings,
                        )
                        output = result.output
                    except AiResponseInvalid as error:
                        progress_history = list(run.before_request)
                        first_format_error = error
                        output = await correct_format(model, run, error)
                        first_format_error = None
                        if instruction := attachment_correction(run):
                            # One explicit domain resumption after the no-tool
                            # format correction. Neither rejected nor corrected
                            # prose becomes attachment evidence or history.
                            run.finalizing = False
                            progress_history.append(
                                ModelRequest(parts=[SystemPromptPart(instruction)])
                            )
                            result = await agent(model, run).run(
                                None,
                                deps=run,
                                message_history=progress_history,
                                model_settings=client.settings,
                            )
                            output = result.output
                    if run.knowledge.has_search_evidence:
                        output = AiModelOutput(
                            message=presentation._knowledge_evidence_message(
                                context.system_locale, output.message, run.knowledge
                            ),
                            candidate=None
                            if run.knowledge.degraded_error_codes
                            else output.candidate,
                            request_intent=output.request_intent,
                        )
                    response = presentation._assist_response(
                        output, context.provider.draft(), run.summaries
                    )
            except RuntimeStop:
                response = await final_after_stop(model, run)
            except TimeoutError:
                if first_format_error is not None:
                    # Only the enclosing timeout converts its own cancellation
                    # here. External cancellation remains CancelledError.
                    if state.format_corrections:
                        raise AiProviderError("ai_timeout", transport_category="timeout") from None
                    raise first_format_error from None
                state.stop_reason = policy._STOP_DEADLINE
                context.audit.record_guard(
                    round_index=state.tool_rounds, stop_reason=state.stop_reason
                )
                response = fallback(run)
            return RuntimeOutcome.from_response(
                presentation._with_omission_note(response, run.omitted, context.system_locale)
            )
