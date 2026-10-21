"""Public model adapter: preserve wire tools while DLR owns dispatch validation."""

from __future__ import annotations

import json
from dataclasses import replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

DISPATCH_TOOL = "dlr_internal_dispatch"


class DispatchModel(WrapperModel):
    """Map tool names at the framework boundary without altering SDK wire history.

    Only the internal function is registered with the Agent. The model receives
    the original allowlist definitions. All proposed calls, including unknown
    names and malformed raw JSON, reach the same DLR dispatcher. The framework
    still owns iteration, call/result pairing and sequential execution.
    """

    def __init__(self, wrapped: Model, definitions: list[ToolDefinition]):
        super().__init__(wrapped)
        self.definitions = definitions

    @staticmethod
    def wire_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
        names: dict[str, str] = {}
        result: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelResponse):
                response_parts = []
                for part in message.parts:
                    if isinstance(part, ToolCallPart) and part.tool_name == DISPATCH_TOOL:
                        envelope = part.args_as_dict()
                        names[part.tool_call_id] = envelope["name"]
                        part = replace(part, tool_name=envelope["name"], args=envelope["arguments"])
                    response_parts.append(part)
                message = replace(message, parts=response_parts)
            elif isinstance(message, ModelRequest):
                request_parts = [
                    replace(part, tool_name=names[part.tool_call_id])
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
        parameters = replace(model_request_parameters, function_tools=self.definitions)
        response = await self.wrapped.request(
            self.wire_messages(messages), model_settings, parameters
        )
        return replace(
            response,
            parts=[
                replace(
                    part,
                    tool_name=DISPATCH_TOOL,
                    args={
                        "name": part.tool_name,
                        "arguments": part.args
                        if isinstance(part.args, str)
                        else json.dumps(part.args or {}, ensure_ascii=False),
                    },
                )
                if isinstance(part, ToolCallPart)
                else part
                for part in response.parts
            ],
        )
