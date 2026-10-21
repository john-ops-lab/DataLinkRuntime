"""Public model adapter: preserve wire tools while DLR owns dispatch validation."""

from __future__ import annotations

import json
from dataclasses import replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from dlr.control.ai import tools
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.provider_config import Protocol

DISPATCH_TOOL = "dlr_internal_dispatch"


class DispatchModel(WrapperModel):
    """Map tool names at the framework boundary without altering SDK wire history.

    Only the internal function is registered with the Agent. The model receives
    the original allowlist definitions. All proposed calls, including unknown
    names and malformed raw JSON, reach the same DLR dispatcher. The framework
    still owns iteration, call/result pairing and sequential execution.
    """

    def __init__(
        self,
        wrapped: Model,
        definitions: list[ToolDefinition],
        *,
        api_key: str | None = None,
        redact_values: tuple[str, ...] = (),
        protocol: Protocol = "openai_compatible",
    ):
        super().__init__(wrapped)
        self.definitions = definitions
        self.api_key, self.redact_values = api_key, redact_values
        self.response_index = 0
        self.protocol = protocol

    def wire_messages(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        names: dict[str, tuple[str, str]] = {}
        result: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelResponse):
                signed = any(
                    isinstance(part, ThinkingPart) and part.signature for part in message.parts
                )
                response_parts = []
                for part in message.parts:
                    if isinstance(part, ToolCallPart) and part.tool_name == DISPATCH_TOOL:
                        envelope = part.args_as_dict()
                        names[part.tool_call_id] = envelope["name"], envelope["wire_id"]
                        raw = envelope["arguments"]
                        safe = tools.sanitize_text(
                            raw, self.api_key, 4000, extra_values=self.redact_values
                        )
                        if signed and safe != raw:
                            raise AiResponseInvalid("output_safety", "secret_reflection")
                        if self.protocol != "openai_compatible":
                            # Only a protocol-valid echo is sent for a rejected
                            # native argument shape. The dispatcher already saw
                            # the original bytes and recorded the real failure.
                            parsed = json.loads(safe)
                            if not isinstance(parsed, dict):
                                if signed:
                                    raise AiResponseInvalid("provider_envelope", "invalid_shape")
                                safe = "{}"
                        part = replace(
                            part,
                            tool_name=envelope["name"],
                            args=safe,
                            tool_call_id=envelope["wire_id"],
                        )
                    response_parts.append(part)
                message = replace(message, parts=response_parts)
            elif isinstance(message, ModelRequest):
                request_parts = [
                    replace(
                        part,
                        tool_name=names[part.tool_call_id][0],
                        tool_call_id=names[part.tool_call_id][1],
                    )
                    if isinstance(part, ToolReturnPart) and part.tool_call_id in names
                    else part
                    for part in message.parts
                ]
                message = replace(message, parts=request_parts)
            result.append(message)
        return result

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        enabled = any(
            tool.name == DISPATCH_TOOL for tool in model_request_parameters.function_tools
        )
        parameters = replace(
            model_request_parameters, function_tools=self.definitions if enabled else []
        )
        response = await self.wrapped.request(
            self.wire_messages(messages), model_settings, parameters
        )
        self.response_index += 1
        return replace(
            response,
            parts=[
                replace(
                    part,
                    tool_name=DISPATCH_TOOL,
                    tool_call_id=f"dlr-{self.response_index}-{index}",
                    args={
                        "name": part.tool_name,
                        "wire_id": part.tool_call_id,
                        "arguments": part.args
                        if isinstance(part.args, str)
                        else json.dumps({} if part.args is None else part.args, ensure_ascii=False),
                    },
                )
                if isinstance(part, ToolCallPart)
                else part
                for index, part in enumerate(response.parts)
            ],
        )
