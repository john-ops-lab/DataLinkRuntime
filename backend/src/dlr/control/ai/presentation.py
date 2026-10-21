"""Shared response presentation driven by server-owned domain evidence."""

from dlr.control.ai import tools as tools_service
from dlr.control.ai.i18n import (
    _fallback_message as _fallback_message,
)
from dlr.control.ai.i18n import (
    _knowledge_evidence_message as _knowledge_evidence_message,
)
from dlr.control.ai.i18n import (
    _transparent_knowledge_message as _transparent_knowledge_message,
)
from dlr.control.ai.i18n import (
    omission_note,
)
from dlr.control.ai.policy import (
    _AssistToolState,
)
from dlr.control.schemas.ai import (
    AiAssistResponse,
    AiModelOutput,
    AiSettingDraft,
    AiToolCallSummary,
)


def _tool_call_summary(execution: tools_service.ToolExecution) -> AiToolCallSummary:
    return AiToolCallSummary(
        tool_name=execution.tool_name,
        status=execution.status,  # type: ignore[arg-type]
        args_summary=execution.args_summary,
        result_summary=execution.result_summary,
        error_code=execution.error_code,
        duration_ms=execution.duration_ms,
        result_truncated=execution.result_truncated,
        result_size=execution.result_size,
        source=execution.source,
    )


def _assist_response(
    output: AiModelOutput,
    draft: AiSettingDraft,
    tool_calls: list[AiToolCallSummary],
) -> AiAssistResponse:
    return AiAssistResponse(
        message=output.message,
        candidate=output.candidate,
        request_intent=output.request_intent,
        provider=draft.provider,
        model=draft.model,
        tool_calls=tool_calls,
    )


def _with_omission_note(
    response: AiAssistResponse, omitted: int, system_locale: str
) -> AiAssistResponse:
    if omitted:
        note = omission_note(omitted, system_locale)
        response.message = f"{note}\n\n{response.message}"
    return response


def _fallback_assist_response(
    system_locale: str,
    stop_reason: str,
    draft: AiSettingDraft,
    tool_calls: list[AiToolCallSummary],
    *,
    state: _AssistToolState,
) -> AiAssistResponse:
    response = AiAssistResponse(
        message=_fallback_message(system_locale, stop_reason, tool_calls),
        candidate=None,
        provider=draft.provider,
        model=draft.model,
        tool_calls=tool_calls,
    )
    state.server_fallback_returned = True
    return response
