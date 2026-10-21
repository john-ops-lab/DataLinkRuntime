"""Conservative, per-call context guard; estimates are not tokenizer counts."""

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from dlr.common.config import settings
from dlr.control.ai import provider_config as providers
from dlr.control.ai.attachment_reading import AttachmentMedia
from dlr.control.ai.output_schema import code_only_output_schema, transport_output_schema
from dlr.control.schemas.ai import AiSettingDraft

# Only models with a verified minimum context window belong here. All other
# models, including custom endpoints, use the configurable conservative bound.
KNOWN_WINDOWS: dict[tuple[str, str], int] = {}
OUTPUT_RESERVE_TOKENS = 8192
SAFETY_RESERVE_TOKENS = 2048
IMAGE_RESERVE_TOKENS = 8192
MESSAGE_OVERHEAD_TOKENS = 32
TOOL_OVERHEAD_TOKENS = 128
BYTES_PER_ESTIMATED_TOKEN = 2
REQUEST_PREFIX = "DLR_REQUEST_CONTEXT_V1\n"
CallPurpose = Literal["assist_initial", "assist_followup", "assist_finalization"]
ESTIMATION_METHOD = "utf8_json_bytes_div2_plus_fixed_reserves_v1"
# A protocol-independent estimate for the histories DLR emits. These margins
# cover wrappers, not variable text/signature/media lengths. Actual SDK bytes
# remain subject to the transport's independent hard limit.
PAYLOAD_ROOT_OVERHEAD = 1024
PAYLOAD_ROW_OVERHEAD = 64
PAYLOAD_PART_OVERHEAD = 256
PAYLOAD_TOOL_OVERHEAD = 256
_INTERNAL_COUNTS = frozenset({"private_utf8_bytes", "private_ascii_bytes", "response_part_count"})


@dataclass(frozen=True)
class BudgetDiagnostics:
    """Safe counters for explaining a decision without retaining request text."""

    purpose: CallPurpose
    window_tokens: int
    window_source: Literal["verified_model", "configured_default"]
    estimated_before_tokens: int
    estimated_after_tokens: int
    system_tokens: int
    conversation_tokens: int
    tool_message_tokens: int
    tool_definition_tokens: int
    output_reserve_tokens: int
    safety_reserve_tokens: int
    omitted_history_messages: int
    omitted_reference_items: int
    omitted_images: int
    payload_bytes: int = 0
    estimated_payload_bytes: int = 0
    max_payload_bytes: int = providers.MAX_PROVIDER_REQUEST_BYTES
    schema_tokens: int = 0
    image_count: int = 0
    pdf_page_count: int = 0
    media_bytes: int = 0
    estimation_method: str = ESTIMATION_METHOD


@dataclass(frozen=True)
class BudgetResult:
    fits: bool
    diagnostics: BudgetDiagnostics
    omitted_materials: int = 0


def _text_tokens(value: object) -> int:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return math.ceil(len(encoded.encode("utf-8")) / BYTES_PER_ESTIMATED_TOKEN)


def _media_estimate(value: object) -> tuple[object, int]:
    """Replace internal body bytes with conservative visual reserves recursively."""
    if isinstance(value, dict):
        if value.get("type") == "dlr_media" and isinstance(value.get("media"), AttachmentMedia):
            media = value["media"]
            reserve = (
                (media.page_count or 1) * (IMAGE_RESERVE_TOKENS + 4096)
                + math.ceil(media.size_bytes / BYTES_PER_ESTIMATED_TOKEN)
                if media.content_type == "application/pdf"
                else IMAGE_RESERVE_TOKENS
            )
            return {"type": "media", "source": media.source()}, reserve
        if value.get("type") == "image_url":
            return {"type": "image", "reserve": IMAGE_RESERVE_TOKENS}, IMAGE_RESERVE_TOKENS
        result: dict[str, object] = {}
        total = 0
        for key, item in value.items():
            converted, reserve = _media_estimate(item)
            result[key] = converted
            total += reserve
        return result, total
    if isinstance(value, list):
        items: list[object] = []
        total = 0
        for item in value:
            converted, reserve = _media_estimate(item)
            items.append(converted)
            total += reserve
        return items, total
    return value, 0


def _media_counts(value: object) -> tuple[int, int, int]:
    if isinstance(value, dict):
        media = value.get("media")
        if value.get("type") == "dlr_media" and isinstance(media, AttachmentMedia):
            return (
                0 if media.content_type == "application/pdf" else 1,
                (media.page_count or 1) if media.content_type == "application/pdf" else 0,
                media.size_bytes,
            )
        if value.get("type") == "image_url":
            image = value.get("image_url")
            url = image.get("url", "") if isinstance(image, dict) else ""
            size = 0
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                body = url.split(";base64,", 1)[1]
                size = len(body) * 3 // 4 - len(body) + len(body.rstrip("="))
            return 1, 0, max(0, size)
        values: list[object] = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        return 0, 0, 0
    counts = [_media_counts(item) for item in values]
    return tuple(sum(items) for items in zip(*counts, strict=True)) if counts else (0, 0, 0)


def _message_tokens(message: providers.JsonObject) -> int:
    content, reserve = _media_estimate(message.get("content"))
    metadata = {
        key: value
        for key, value in message.items()
        if key != "content" and key not in _INTERNAL_COUNTS
    }
    private = math.ceil(_internal_count(message, "private_utf8_bytes") / BYTES_PER_ESTIMATED_TOKEN)
    return (
        MESSAGE_OVERHEAD_TOKENS + _text_tokens(metadata) + _text_tokens(content) + reserve + private
    )


def _internal_count(message: providers.JsonObject, name: str) -> int:
    value = message.get(name, 0)
    assert isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value


def _payload_value(value: object) -> tuple[object, int]:
    """Measure each base64 occurrence without copying blobs into another JSON."""
    if isinstance(value, dict):
        media = value.get("media")
        if value.get("type") == "dlr_media" and isinstance(media, AttachmentMedia):
            return (
                {"media_type": media.content_type, "data": "", "source": media.source()},
                len(media.data_base64),
            )
        image = value.get("image_url")
        if value.get("type") == "image_url" and isinstance(image, dict):
            url = image.get("url")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                header, body = url.split(";base64,", 1)
                return (
                    {**value, "image_url": {**image, "url": header + ";base64,"}},
                    len(body),
                )
        converted: dict[str, object] = {}
        total = 0
        for key, item in value.items():
            converted[key], count = _payload_value(item)
            total += count
        return converted, total
    if isinstance(value, list | tuple):
        items: list[object] = []
        total = 0
        for item in value:
            converted_item, count = _payload_value(item)
            items.append(converted_item)
            total += count
        return items, total
    return value, 0


@lru_cache(maxsize=1)
def _transport_schema_bytes() -> int:
    # Only an integer is cached; consumers always get a fresh schema object.
    return len(json.dumps(transport_output_schema(), ensure_ascii=True).encode("ascii"))


def estimated_payload_bytes(
    draft: AiSettingDraft,
    messages: list[providers.JsonObject],
    tools: list[providers.JsonObject] | None,
) -> int:
    """Estimate DLR-generated SDK histories without any protocol encoder.

    ASCII escaping includes Google's larger encoding of non-ASCII strings.
    Tool objects may become JSON inside a JSON string on OpenAI endpoints.
    Near a limit, this estimate can remove an optional whole item earlier.
    It is not an upper-bound promise for arbitrary SDK message extensions.
    """
    rows: list[dict[str, object]] = []
    private = parts = blobs = 0
    for message in messages:
        converted, blob_bytes = _payload_value(
            {key: value for key, value in message.items() if key not in _INTERNAL_COUNTS}
        )
        assert isinstance(converted, dict)
        content = converted.get("content")
        if message.get("role") == "tool" and content is not None and not isinstance(content, str):
            converted["content"] = json.dumps(content, ensure_ascii=True)
        rows.append(converted)
        blobs += blob_bytes
        private += _internal_count(message, "private_ascii_bytes")
        calls = message.get("tool_calls")
        parts += max(
            _internal_count(message, "response_part_count"),
            len(content) if isinstance(content, list) else 1,
            len(calls) if isinstance(calls, list) else 0,
        )
    canonical = {
        "model": draft.model,
        "reasoning_mode": draft.reasoning_mode,
        "reasoning_effort": draft.reasoning_effort,
        "messages": rows,
        "tools": tools,
        "output_schema": None,
    }
    return (
        len(json.dumps(canonical, ensure_ascii=True).encode("ascii"))
        + _transport_schema_bytes()
        + blobs
        + private
        + PAYLOAD_ROOT_OVERHEAD
        + PAYLOAD_ROW_OVERHEAD * len(rows)
        + PAYLOAD_PART_OVERHEAD * parts
        + PAYLOAD_TOOL_OVERHEAD * len(tools or [])
    )


def _schema_tokens(adapter: providers.ProviderAdapter | None) -> int:
    return (
        _text_tokens(code_only_output_schema())
        if adapter is not None and adapter.structured_output == "json_schema"
        else 0
    )


def estimate_tokens(
    messages: list[providers.JsonObject],
    tools: list[providers.JsonObject] | None,
    *,
    purpose: CallPurpose = "assist_initial",
    adapter: providers.ProviderAdapter | None = None,
) -> int:
    return (
        OUTPUT_RESERVE_TOKENS
        + SAFETY_RESERVE_TOKENS
        + sum(_message_tokens(message) for message in messages)
        + (TOOL_OVERHEAD_TOKENS + _text_tokens(tools) if tools else 0)
        + _schema_tokens(adapter)
    )


def _diagnostic_parts(
    messages: list[providers.JsonObject], tools: list[providers.JsonObject] | None
) -> tuple[int, int, int, int]:
    system = conversation = tool_messages = 0
    for message in messages:
        amount = _message_tokens(message)
        role = message.get("role")
        if role == "system":
            system += amount
        elif role == "tool":
            tool_messages += amount
        else:
            conversation += amount
    definitions = TOOL_OVERHEAD_TOKENS + _text_tokens(tools) if tools else 0
    return system, conversation, tool_messages, definitions


def _request_message(messages: list[providers.JsonObject]) -> providers.JsonObject | None:
    # The current envelope is the last matching user message. An older user
    # utterance may contain the same literal prefix as untrusted text.
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith(REQUEST_PREFIX):
            return message
        if (
            isinstance(content, list)
            and content
            and isinstance(content[0], dict)
            and str(content[0].get("text", "")).startswith(REQUEST_PREFIX)
        ):
            return message
    return None


def _drop_reference(message: providers.JsonObject) -> bool:
    content = message.get("content")
    text_part = content[0] if isinstance(content, list) and content else None
    text = (
        content
        if isinstance(content, str)
        else text_part.get("text")
        if isinstance(text_part, dict)
        else None
    )
    if not isinstance(text, str) or not text.startswith(REQUEST_PREFIX):
        return False
    data = json.loads(text[len(REQUEST_PREFIX) :])
    reference = data.get("UNTRUSTED_REFERENCE_MATERIAL")
    if not isinstance(reference, dict):
        return False
    for name in ("attachments", "context_snippets"):
        items = reference.get(name)
        if isinstance(items, list) and items:
            items.pop()  # whole item, never a partial user-provided document
            if not items:
                del reference[name]
            if not reference:
                del data["UNTRUSTED_REFERENCE_MATERIAL"]
            updated = REQUEST_PREFIX + json.dumps(data, ensure_ascii=False, sort_keys=True)
            if isinstance(content, str):
                message["content"] = updated
            else:
                assert isinstance(text_part, dict)
                text_part["text"] = updated
            return True
    return False


def prepare_call(
    draft: AiSettingDraft,
    messages: list[providers.JsonObject],
    tools: list[providers.JsonObject] | None,
    *,
    purpose: CallPurpose = "assist_initial",
    adapter: providers.ProviderAdapter | None = None,
) -> BudgetResult:
    """Mutate only optional whole history/reference/image items to fit a call."""
    adapter = providers.resolve_output_adapter(
        draft,
        providers.resolve_attachment_adapter(
            draft, adapter or providers.get_provider(draft.provider)
        ),
    )
    verified_window = adapter.context_window_tokens or KNOWN_WINDOWS.get(
        (draft.provider, draft.model)
    )
    window = verified_window or settings.ai_context_default_window_tokens
    before = estimate_tokens(messages, tools, purpose=purpose, adapter=adapter)
    omitted_history = omitted_references = omitted_images = 0

    def result(fits: bool) -> BudgetResult:
        system, conversation, tool_messages, definitions = _diagnostic_parts(messages, tools)
        output_reserve = OUTPUT_RESERVE_TOKENS
        image_count, pdf_page_count, media_bytes = _media_counts(messages)
        after = (
            output_reserve
            + SAFETY_RESERVE_TOKENS
            + system
            + conversation
            + tool_messages
            + definitions
            + _schema_tokens(adapter)
        )
        return BudgetResult(
            fits=fits,
            omitted_materials=omitted_history + omitted_references + omitted_images,
            diagnostics=BudgetDiagnostics(
                purpose=purpose,
                window_tokens=window,
                window_source="verified_model" if verified_window else "configured_default",
                estimated_before_tokens=before,
                estimated_after_tokens=after,
                system_tokens=system,
                conversation_tokens=conversation,
                tool_message_tokens=tool_messages,
                tool_definition_tokens=definitions,
                output_reserve_tokens=output_reserve,
                safety_reserve_tokens=SAFETY_RESERVE_TOKENS,
                omitted_history_messages=omitted_history,
                omitted_reference_items=omitted_references,
                omitted_images=omitted_images,
                estimated_payload_bytes=estimated_payload_bytes(draft, messages, tools),
                max_payload_bytes=adapter.max_request_bytes,
                schema_tokens=_schema_tokens(adapter),
                image_count=image_count,
                pdf_page_count=pdf_page_count,
                media_bytes=media_bytes,
            ),
        )

    while (
        estimate_tokens(messages, tools, purpose=purpose, adapter=adapter) > window
        or estimated_payload_bytes(draft, messages, tools) > adapter.max_request_bytes
    ):
        request = _request_message(messages)
        if request is not None:
            request_index = next(index for index, item in enumerate(messages) if item is request)
            # History ends at the current request. Tool rounds follow it and
            # are retained as indivisible assistant-call/result groups.
            history_indexes = [
                index
                for index in range(request_index)
                if messages[index].get("role") in ("user", "assistant")
            ]
            if history_indexes:
                first = history_indexes[0]
                next_user = next(
                    (
                        index
                        for index in history_indexes[1:]
                        if messages[index].get("role") == "user"
                    ),
                    request_index,
                )
                del messages[first:next_user]
                omitted_history += next_user - first
                continue
            if _drop_reference(request):
                omitted_references += 1
                continue
            content = request.get("content")
            if isinstance(content, list) and len(content) > 1:
                content.pop()
                omitted_images += 1
                continue
        return result(False)
    return result(True)


def has_native_image(messages: list[providers.JsonObject]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict)
            and (
                part.get("type") == "image_url"
                or part.get("type") == "dlr_media"
                and isinstance(part.get("media"), AttachmentMedia)
                and part["media"].content_type.startswith("image/")
            )
            for part in content
        ):
            return True
    return False
