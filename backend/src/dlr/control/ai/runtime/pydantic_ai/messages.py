"""Public framework messages bridged to DLR's shared context-budget policy."""

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from dlr.control.ai import context_budget
from dlr.control.ai.attachment_reading import AttachmentMedia
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.provider_config import JsonObject, ProviderSnapshot


def user_content(content: object) -> str | list[str | BinaryContent]:
    if isinstance(content, str):
        return content
    assert isinstance(content, list)
    converted: list[str | BinaryContent] = []
    for item in content:
        assert isinstance(item, dict)
        if item.get("type") == "text":
            converted.append(str(item["text"]))
        elif item.get("type") == "image_url":
            url = item["image_url"]["url"]
            header, body = url.split(";base64,", 1)
            converted.append(BinaryContent(base64.b64decode(body), media_type=header[5:]))
        elif item.get("type") == "dlr_media":
            media = item["media"]
            assert isinstance(media, AttachmentMedia)
            converted.append(
                BinaryContent(base64.b64decode(media.data_base64), media_type=media.content_type)
            )
    return converted


def initial_messages(messages: list[JsonObject]) -> list[ModelMessage]:
    result: list[ModelMessage] = []
    for message in messages:
        role, content = message["role"], message.get("content", "")
        if role == "system":
            result.append(ModelRequest(parts=[SystemPromptPart(str(content))]))
        elif role == "assistant":
            result.append(ModelResponse(parts=[TextPart(str(content))]))
        else:
            result.append(ModelRequest(parts=[UserPromptPart(user_content(content))]))
    return result


def _content(value: Any, media: dict[str, AttachmentMedia]) -> Any:
    if isinstance(value, BinaryContent):
        found = media.get(hashlib.sha256(value.data).hexdigest())
        if found is not None:
            return {"type": "dlr_media", "media": found}
        return {"type": "image_url", "image_url": {"url": value.data_uri}}
    if isinstance(value, list | tuple):
        return [
            ({"type": "text", "text": item} if isinstance(item, str) else _content(item, media))
            for item in value
        ]
    return value


def budget_view(
    messages: list[ModelMessage],
    media: dict[str, AttachmentMedia],
    wire_view: Callable[[list[ModelMessage]], list[ModelMessage]] | None = None,
) -> tuple[list[JsonObject], dict[int, tuple[ModelMessage, int]]]:
    result: list[JsonObject] = []
    origins: dict[int, tuple[ModelMessage, int]] = {}
    wire = wire_view(messages) if wire_view is not None else messages
    for original, message in zip(messages, wire, strict=True):
        if isinstance(message, ModelResponse):
            calls = [part for part in message.parts if isinstance(part, ToolCallPart)]
            row: JsonObject = {"role": "assistant", "content": message.text or None}
            if calls:
                row["tool_calls"] = [
                    {
                        "id": call.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": call.args
                            if isinstance(call.args, str)
                            else json.dumps(call.args, ensure_ascii=False),
                        },
                    }
                    for call in calls
                ]
            private_utf8 = private_ascii = 0
            for response_part in message.parts:
                private: dict[str, object] = {}
                if isinstance(response_part, ThinkingPart):
                    private = {
                        "content": response_part.content,
                        "signature": response_part.signature,
                    }
                if (
                    isinstance(response_part, ThinkingPart | TextPart | ToolCallPart)
                    and response_part.provider_details
                ):
                    private["provider_details"] = response_part.provider_details
                if private:
                    try:
                        private_utf8 += len(json.dumps(private, ensure_ascii=False).encode("utf-8"))
                        private_ascii += len(json.dumps(private, ensure_ascii=True).encode("ascii"))
                    except (ValueError, TypeError, RecursionError, UnicodeError):
                        raise AiResponseInvalid("provider_envelope", "invalid_shape") from None
            row.update(
                private_utf8_bytes=private_utf8,
                private_ascii_bytes=private_ascii,
                response_part_count=len(message.parts),
            )
            result.append(row)
            origins[id(row)] = original, -1
        elif isinstance(message, ModelRequest):
            for index, part in enumerate(message.parts):
                if isinstance(part, SystemPromptPart):
                    row = {"role": "system", "content": part.content}
                elif isinstance(part, UserPromptPart):
                    row = {"role": "user", "content": _content(part.content, media)}
                elif isinstance(part, ToolReturnPart):
                    row = {
                        "role": "tool",
                        "tool_call_id": part.tool_call_id,
                        "name": part.tool_name,
                        "content": _content(part.content, media),
                    }
                elif isinstance(part, RetryPromptPart):
                    row = {"role": "system", "content": str(part.content)}
                else:
                    continue
                result.append(row)
                origins[id(row)] = original, index
    return result, origins


def prepare_messages(
    messages: list[ModelMessage],
    snapshot: ProviderSnapshot,
    tools: list[JsonObject] | None,
    media: dict[str, AttachmentMedia],
    *,
    purpose: context_budget.CallPurpose,
    wire_view: Callable[[list[ModelMessage]], list[ModelMessage]] | None = None,
) -> tuple[list[ModelMessage], context_budget.BudgetResult]:
    view, origins = budget_view(messages, media, wire_view)
    budget = context_budget.prepare_call(
        snapshot.draft(), view, tools, purpose=purpose, adapter=snapshot.adapter
    )
    retained = {id(message) for row in view for message, _ in [origins[id(row)]]}
    updates = {
        (id(message), index): row
        for row in view
        for message, index in [origins[id(row)]]
        if index >= 0
    }
    prepared: list[ModelMessage] = []
    for message in messages:
        if id(message) not in retained:
            continue
        if isinstance(message, ModelRequest):
            parts = []
            for index, part in enumerate(message.parts):
                row = updates.get((id(message), index))
                if row is None and isinstance(
                    part, SystemPromptPart | UserPromptPart | ToolReturnPart | RetryPromptPart
                ):
                    # The Agent may merge system and user parts in one request.
                    # A retained sibling must not restore a removed history row.
                    continue
                if row is not None and isinstance(part, UserPromptPart):
                    part = replace(part, content=user_content(row["content"]))
                parts.append(part)
            message = replace(message, parts=parts)
        prepared.append(message)
    return prepared, budget
