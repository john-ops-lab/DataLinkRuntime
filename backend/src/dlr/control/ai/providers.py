"""Small, explicit protocol adapters used by the AI Editor.

OpenAI-compatible services share one adapter, while Anthropic Messages and
Google Gemini use their native request/response shapes. Provider capabilities
are declared here rather than inferred from model names or user URLs.
"""

import json
import re
import socket
from dataclasses import dataclass
from http import client as http_client
from typing import Literal, cast
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from dlr.common.config import settings
from dlr.control.schemas.ai import (
    AiProvider,
    AiSettingDraft,
    ReasoningEffort,
    ReasoningMode,
    contains_unicode_surrogate,
)

MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024

JsonObject = dict[str, object]
StructuredOutput = Literal["json_object", "prompt_only"]
ReasoningStyle = Literal["openai_effort", "thinking", "unsupported"]
Protocol = Literal["openai_compatible", "anthropic", "gemini"]


@dataclass(frozen=True)
class NormalizedToolCall:
    """One validated provider tool call with typed string fields."""

    id: str
    name: str
    arguments: str


class AiProviderError(Exception):
    """Sanitized provider failure carrying only a stable public error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirectHandler(url_request.HTTPRedirectHandler):
    """Never forward API keys, prompts or Working Copies to another URL."""

    def redirect_request(
        self,
        req: url_request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_NO_REDIRECT_OPENER = url_request.build_opener(_NoRedirectHandler())


@dataclass(frozen=True)
class ProviderAdapter:
    provider: AiProvider
    structured_output: StructuredOutput
    reasoning_style: ReasoningStyle
    reasoning_efforts: frozenset[ReasoningEffort] = frozenset()
    require_effort_when_enabled: bool = False
    split_reasoning: bool = False
    # M5.7 Wave B2: explicit native-attachment capability. "Multimodal" is
    # never assumed: only entries in this table enable provider-native input.
    images_native: bool = False
    files_native: bool = False
    # M5.7 Wave C1: explicit read-only Tool Call capability. Only providers
    # whose Chat Completions contract is known to accept ``tools`` offer the
    # whitelisted function definitions; the flag is capability-table truth,
    # never inferred from the model id. Providers without the flag keep the
    # exact single-shot protocol (no ``tools`` payload key).
    tools_supported: bool = False
    protocol: Protocol = "openai_compatible"


PROVIDERS: dict[AiProvider, ProviderAdapter] = {
    # A legacy Candidate runtime_config echo is intentionally an arbitrary JSON
    # object. OpenAI strict Structured Outputs requires closed object schemas,
    # so a strict json_schema would misrepresent that compatibility envelope.
    # Use JSON mode plus the explicit prompt and mandatory local strict
    # validation instead; M5.8-003 never lets AI apply this field.
    "openai": ProviderAdapter(
        "openai",
        "json_object",
        "openai_effort",
        frozenset(("low", "medium", "high", "xhigh")),
        True,
        images_native=True,
        tools_supported=True,
    ),
    "anthropic": ProviderAdapter(
        "anthropic",
        "prompt_only",
        "unsupported",
        images_native=True,
        tools_supported=True,
        protocol="anthropic",
    ),
    "gemini": ProviderAdapter(
        "gemini",
        "prompt_only",
        "unsupported",
        images_native=True,
        tools_supported=True,
        protocol="gemini",
    ),
    "deepseek": ProviderAdapter(
        "deepseek",
        "json_object",
        "thinking",
        frozenset(("high", "max")),
    ),
    # These services expose their documented OpenAI-compatible Chat
    # Completions endpoints. They remain distinct catalog entries so each can
    # carry explicit capabilities and a provider-specific default URL.
    "qwen": ProviderAdapter("qwen", "json_object", "unsupported", tools_supported=True),
    # Kimi exposes the OpenAI-compatible thinking switch for models that
    # support it; upstream rejection is converted to ai_reasoning_unsupported
    # without maintaining a brittle model-name allowlist.
    "kimi": ProviderAdapter("kimi", "json_object", "thinking"),
    # MiniMax can return reasoning separately when reasoning_split is true.
    "minimax": ProviderAdapter("minimax", "prompt_only", "unsupported", split_reasoning=True),
    "glm": ProviderAdapter("glm", "json_object", "unsupported", tools_supported=True),
    "doubao": ProviderAdapter("doubao", "json_object", "unsupported", tools_supported=True),
    "hunyuan": ProviderAdapter("hunyuan", "json_object", "unsupported", tools_supported=True),
    "openrouter": ProviderAdapter("openrouter", "json_object", "unsupported", tools_supported=True),
    "siliconflow": ProviderAdapter(
        "siliconflow", "json_object", "unsupported", tools_supported=True
    ),
    "ollama": ProviderAdapter("ollama", "json_object", "unsupported", tools_supported=True),
    # The custom OpenAI-compatible family is exercised end-to-end by the
    # compose-smoke fake Provider (tool-call -> tool result -> final answer).
    "custom_openai_compatible": ProviderAdapter(
        "custom_openai_compatible", "prompt_only", "unsupported", tools_supported=True
    ),
}

PROVIDER_DISPLAY_NAMES: dict[AiProvider, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic Claude",
    "gemini": "Google Gemini",
    "deepseek": "DeepSeek",
    "qwen": "Alibaba Qwen",
    "kimi": "Moonshot Kimi",
    "minimax": "MiniMax",
    "glm": "GLM",
    "doubao": "Doubao",
    "hunyuan": "Hunyuan",
    "openrouter": "OpenRouter",
    "siliconflow": "SiliconFlow",
    "ollama": "Ollama",
    "custom_openai_compatible": "Custom provider",
}

PROVIDER_DEFAULT_BASE_URLS: dict[AiProvider, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode",
    "kimi": "https://api.moonshot.cn",
    "minimax": "https://api.minimax.chat",
    "glm": "https://open.bigmodel.cn/api/paas",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "hunyuan": "https://api.hunyuan.cloud.tencent.com",
    "openrouter": "https://openrouter.ai/api",
    "siliconflow": "https://api.siliconflow.cn",
    "ollama": "http://ollama:11434",
    "custom_openai_compatible": "",
}


def get_provider(provider: AiProvider) -> ProviderAdapter:
    return PROVIDERS[provider]


def custom_provider_adapter(
    protocol: Protocol,
    *,
    images_native: bool,
    files_native: bool,
    tools_supported: bool,
) -> ProviderAdapter:
    """Build an explicit adapter for one persisted custom profile."""
    return ProviderAdapter(
        "custom_openai_compatible",
        "prompt_only",
        "unsupported",
        images_native=images_native,
        files_native=files_native,
        tools_supported=tools_supported,
        protocol=protocol,
    )


def validate_reasoning(
    adapter: ProviderAdapter,
    mode: ReasoningMode,
    effort: ReasoningEffort | None,
) -> None:
    """Reject explicit choices that cannot be mapped without guessing."""
    if mode == "default":
        if effort is not None:
            raise AiProviderError("ai_reasoning_unsupported")
        return
    if mode == "disabled" and effort is not None:
        raise AiProviderError("ai_reasoning_unsupported")
    if adapter.reasoning_style == "unsupported":
        raise AiProviderError("ai_reasoning_unsupported")
    if mode == "enabled" and adapter.require_effort_when_enabled and effort is None:
        raise AiProviderError("ai_reasoning_unsupported")
    if effort is not None and effort not in adapter.reasoning_efforts:
        raise AiProviderError("ai_reasoning_unsupported")
    if adapter.reasoning_style == "openai_effort" and mode == "disabled":
        # There is no model-independent OpenAI Chat Completions switch that
        # reliably means "disable reasoning". Do not silently fake one.
        raise AiProviderError("ai_reasoning_unsupported")


def normalize_base_url(base_url: str) -> str:
    """Return one safe OpenAI-compatible root without a trailing ``/v1``.

    Providers conventionally expose the same endpoints below ``/v1`` while
    users commonly paste either the service root or the versioned root. URL
    parsing keeps the scheme delimiter intact while collapsing repeated path
    slashes and repeated trailing ``v1`` segments.
    """
    parts = url_parse.urlsplit(base_url.strip())
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    path_parts = [part for part in path.split("/") if part]
    while path_parts and path_parts[-1].lower() == "v1":
        path_parts.pop()
    normalized_path = "/" + "/".join(path_parts) if path_parts else ""
    return url_parse.urlunsplit((parts.scheme, parts.netloc, normalized_path, "", ""))


def _endpoint(base_url: str, path: str) -> str:
    """Append one normalized OpenAI endpoint path to a provider root."""
    root = normalize_base_url(base_url)
    return f"{root}/{path.lstrip('/')}"


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _protocol_headers(adapter: ProviderAdapter, api_key: str | None) -> dict[str, str]:
    """Build protocol-specific auth headers without putting keys in URLs."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key is None:
        return headers
    if adapter.protocol == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif adapter.protocol == "gemini":
        headers["x-goog-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def load_json_strict(value: str | bytes) -> object:
    """Parse untrusted provider JSON without ambiguous/non-standard values."""
    return cast(
        object,
        json.loads(
            value,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        ),
    )


def _is_dns_resolution_failure(error: BaseException) -> bool:
    """True only for hostname resolution failures (``socket.gaierror``).

    ``urllib`` wraps most low-level errors in ``URLError``; DNS failures reach
    the caller as ``URLError(reason=gaierror)`` while other transport errors
    (connection refused, TLS handshake) carry a different reason. The plain
    ``gaierror`` case is kept for direct OSError propagation.
    """
    return isinstance(error, socket.gaierror) or isinstance(
        getattr(error, "reason", None), socket.gaierror
    )


def _request_json(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: JsonObject | None = None,
    *,
    not_found_code: str,
    reasoning_explicit: bool = False,
    model_discovery: bool = False,
    image_input: bool = False,
) -> object:
    """Bounded JSON HTTP request with fully sanitized failure mapping."""
    try:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = url_request.Request(url, data=data, headers=headers, method=method)
        with _NO_REDIRECT_OPENER.open(
            request,
            timeout=settings.ai_provider_timeout_seconds,  # noqa: S310 - admin-configured URL
        ) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
    except url_error.HTTPError as error:
        if error.code in (401, 403):
            raise AiProviderError("ai_auth_failed") from None
        if error.code in (408, 504):
            raise AiProviderError("ai_timeout") from None
        if error.code == 404:
            raise AiProviderError(not_found_code) from None
        # M5.7 Wave B2: a Provider that rejects a native image payload
        # (wrong model family, image size policy) gets its own actionable
        # code instead of a generic transport error.
        if image_input and error.code in (400, 422):
            raise AiProviderError("ai_attachment_image_unsupported") from None
        if reasoning_explicit and error.code in (400, 422):
            raise AiProviderError("ai_reasoning_unsupported") from None
        # A responding Provider that rejects the discovery request (missing or
        # unsupported /v1/models) is not unreachable; the two failures get
        # distinct actionable messages.
        if model_discovery and error.code in (400, 405, 422):
            raise AiProviderError("ai_models_not_supported") from None
        raise AiProviderError("ai_provider_unreachable") from None
    except TimeoutError:
        raise AiProviderError("ai_timeout") from None
    except (url_error.URLError, http_client.HTTPException, OSError, ValueError) as error:
        # M5.5.3: keep DNS resolution failures distinguishable from generic
        # transport failures so deployment docs can guide the operator to the
        # right layer (DNS vs TCP/TLS) instead of a single catch-all message.
        if _is_dns_resolution_failure(error):
            raise AiProviderError("ai_provider_dns_failed") from None
        raise AiProviderError("ai_provider_unreachable") from None
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise AiProviderError("ai_response_invalid")
    try:
        return load_json_strict(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise AiProviderError("ai_response_invalid") from None


def _apply_reasoning(
    payload: JsonObject, setting: AiSettingDraft, adapter: ProviderAdapter | None = None
) -> None:
    adapter = adapter or get_provider(setting.provider)
    validate_reasoning(adapter, setting.reasoning_mode, setting.reasoning_effort)
    if adapter.split_reasoning:
        # Output separation only; this does not enable or disable reasoning.
        payload["reasoning_split"] = True
    if setting.reasoning_mode == "default":
        return
    if adapter.reasoning_style == "openai_effort":
        # validate_reasoning requires an explicit supported effort here.
        payload["reasoning_effort"] = setting.reasoning_effort
    elif adapter.reasoning_style == "thinking":
        payload["thinking"] = {"type": setting.reasoning_mode}
        if setting.reasoning_effort is not None:
            payload["reasoning_effort"] = setting.reasoning_effort


def build_chat_payload(
    setting: AiSettingDraft,
    messages: list[JsonObject],
    *,
    structured: bool,
    adapter: ProviderAdapter | None = None,
) -> JsonObject:
    """Build one non-streaming Chat Completions request."""
    adapter = adapter or get_provider(setting.provider)
    payload: JsonObject = {
        "model": setting.model,
        "messages": messages,
        "stream": False,
    }
    _apply_reasoning(payload, setting, adapter)
    if structured and adapter.structured_output == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _anthropic_content(content: object) -> object:
    """Translate the DLR visible content parts into Anthropic blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    blocks: list[dict[str, object]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            blocks.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                match = re.match(r"data:([^;]+);base64,(.+)", image_url["url"])
                if match:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": match.group(1),
                                "data": match.group(2),
                            },
                        }
                    )
    return blocks or ""


def _gemini_parts(content: object) -> list[JsonObject]:
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        return []
    parts: list[JsonObject] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append({"text": item["text"]})
        elif item.get("type") == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                match = re.match(r"data:([^;]+);base64,(.+)", image_url["url"])
                if match:
                    parts.append(
                        {
                            "inlineData": {
                                "mimeType": match.group(1),
                                "data": match.group(2),
                            }
                        }
                    )
    return parts


def _anthropic_messages(messages: list[JsonObject]) -> tuple[str | None, list[JsonObject]]:
    system_parts: list[str] = []
    result: list[JsonObject] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _content_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id", "unknown"),
                            "content": _content_text(message.get("content")),
                        }
                    ],
                }
            )
            continue
        role_name = "assistant" if role == "assistant" else "user"
        blocks: object = _anthropic_content(message.get("content"))
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            call_blocks = blocks if isinstance(blocks, list) else []
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                try:
                    arguments = load_json_strict(function.get("arguments", "{}"))
                except (ValueError, RecursionError, TypeError):
                    arguments = {}
                call_blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", "unknown"),
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            blocks = call_blocks
        result.append({"role": role_name, "content": blocks})
    return ("\n".join(system_parts) or None), result


def _gemini_contents(messages: list[JsonObject]) -> tuple[JsonObject | None, list[JsonObject]]:
    system_parts: list[JsonObject] = []
    result: list[JsonObject] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.extend(_gemini_parts(message.get("content")))
            continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            tool_name = message.get("name")
            if not isinstance(tool_name, str):
                tool_name = tool_names.get(str(tool_call_id), str(tool_call_id))
            result.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tool_name,
                                "response": {"content": _content_text(message.get("content"))},
                            }
                        }
                    ],
                }
            )
            continue
        role_name = "model" if role == "assistant" else "user"
        parts = _gemini_parts(message.get("content"))
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for call in raw_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                call_id = call.get("id")
                call_name = function.get("name")
                if isinstance(call_id, str) and isinstance(call_name, str):
                    tool_names[call_id] = call_name
                try:
                    arguments = load_json_strict(function.get("arguments", "{}"))
                except (ValueError, RecursionError, TypeError):
                    arguments = {}
                parts.append(
                    {
                        "functionCall": {
                            "name": function.get("name", ""),
                            "args": arguments,
                        }
                    }
                )
        result.append({"role": role_name, "parts": parts})
    system: JsonObject | None = {"parts": system_parts} if system_parts else None
    return system, result


def _anthropic_tools(tools: list[JsonObject]) -> list[JsonObject]:
    converted: list[JsonObject] = []
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict):
            converted.append(
                {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "input_schema": function.get("parameters", {"type": "object"}),
                }
            )
    return converted


def _gemini_tools(tools: list[JsonObject]) -> list[JsonObject]:
    declarations: list[JsonObject] = []
    for tool in tools:
        function = tool.get("function")
        if isinstance(function, dict):
            declarations.append(
                {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {"type": "object"}),
                }
            )
    return [{"functionDeclarations": declarations}]


def _build_protocol_payload(
    setting: AiSettingDraft,
    messages: list[JsonObject],
    *,
    structured: bool,
    adapter: ProviderAdapter,
    tools: list[JsonObject] | None = None,
) -> JsonObject:
    if adapter.protocol == "openai_compatible":
        payload = build_chat_payload(setting, messages, structured=structured, adapter=adapter)
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload
    if adapter.protocol == "anthropic":
        anthropic_system, converted = _anthropic_messages(messages)
        anthropic_payload: JsonObject = {
            "model": setting.model,
            "messages": converted,
            "max_tokens": 4096,
        }
        if anthropic_system is not None:
            anthropic_payload["system"] = anthropic_system
        if tools is not None:
            anthropic_payload["tools"] = _anthropic_tools(tools)
        return anthropic_payload
    gemini_system, converted = _gemini_contents(messages)
    gemini_payload: JsonObject = {"contents": converted}
    if gemini_system is not None:
        gemini_payload["systemInstruction"] = gemini_system
    if structured:
        gemini_payload["generationConfig"] = {"responseMimeType": "application/json"}
    if tools is not None:
        gemini_payload["tools"] = _gemini_tools(tools)
    return gemini_payload


def _strip_thinking_container(content: str) -> str:
    """Remove one or more complete leading ``<think>`` containers only."""
    final = content.strip()
    while final.lower().startswith("<think>"):
        closing = final.lower().find("</think>", len("<think>"))
        if closing < 0:
            raise AiProviderError("ai_response_invalid")
        final = final[closing + len("</think>") :].strip()
    if not final:
        raise AiProviderError("ai_response_invalid")
    return final


def _extract_anthropic_round(
    response: object,
) -> tuple[str | None, list[NormalizedToolCall] | None]:
    if not isinstance(response, dict) or not isinstance(response.get("content"), list):
        raise AiProviderError("ai_response_invalid")
    text_parts: list[str] = []
    tool_calls: list[NormalizedToolCall] = []
    for block in response["content"]:
        if not isinstance(block, dict):
            raise AiProviderError("ai_response_invalid")
        block_type = block.get("type")
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise AiProviderError("ai_response_invalid")
            text_parts.append(block["text"])
        elif block_type == "tool_use":
            name = block.get("name")
            call_id = block.get("id")
            if not isinstance(name, str) or not name.strip() or not isinstance(call_id, str):
                raise AiProviderError("ai_response_invalid")
            try:
                arguments = json.dumps(block.get("input", {}), ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError, RecursionError):
                raise AiProviderError("ai_response_invalid") from None
            tool_calls.append(NormalizedToolCall(call_id, name, arguments))
        else:
            raise AiProviderError("ai_response_invalid")
    if tool_calls:
        return ("".join(text_parts) or None), tool_calls
    if response.get("stop_reason") not in (None, "end_turn"):
        raise AiProviderError("ai_response_invalid")
    return _strip_thinking_container("".join(text_parts)), None


def _extract_gemini_round(
    response: object,
) -> tuple[str | None, list[NormalizedToolCall] | None]:
    if not isinstance(response, dict):
        raise AiProviderError("ai_response_invalid")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise AiProviderError("ai_response_invalid")
    candidate = candidates[0]
    content = candidate.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        raise AiProviderError("ai_response_invalid")
    text_parts: list[str] = []
    tool_calls: list[NormalizedToolCall] = []
    for part in content["parts"]:
        if not isinstance(part, dict):
            raise AiProviderError("ai_response_invalid")
        if isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        elif isinstance(part.get("functionCall"), dict):
            function_call = part["functionCall"]
            name = function_call.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AiProviderError("ai_response_invalid")
            try:
                arguments = json.dumps(
                    function_call.get("args", {}), ensure_ascii=False, sort_keys=True
                )
            except (TypeError, ValueError, RecursionError):
                raise AiProviderError("ai_response_invalid") from None
            # Gemini does not require a client-generated call id. A stable
            # per-response id lets the common DLR tool loop preserve its
            # provider-neutral message contract.
            tool_calls.append(
                NormalizedToolCall(f"gemini-call-{len(tool_calls) + 1}", name, arguments)
            )
        else:
            raise AiProviderError("ai_response_invalid")
    if tool_calls:
        return ("".join(text_parts) or None), tool_calls
    if candidate.get("finishReason") not in (None, "STOP"):
        raise AiProviderError("ai_response_invalid")
    return _strip_thinking_container("".join(text_parts)), None


def extract_final_text(
    provider: AiProvider, response: object, adapter: ProviderAdapter | None = None
) -> str:
    """Normalize one provider fixture into final text only.

    ``reasoning_content`` and ``reasoning_details`` are intentionally never
    read, returned or persisted. All supported providers use the normalized
    Chat Completions ``choices[0].message.content`` for their final answer.
    """
    adapter = adapter or get_provider(provider)
    if adapter.protocol != "openai_compatible":
        final, tool_calls = (
            _extract_anthropic_round(response)
            if adapter.protocol == "anthropic"
            else _extract_gemini_round(response)
        )
        if tool_calls is not None or final is None:
            raise AiProviderError("ai_response_invalid")
        return final
    get_provider(provider)  # validates and documents the selected thin adapter
    if not isinstance(response, dict):
        raise AiProviderError("ai_response_invalid")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AiProviderError("ai_response_invalid")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise AiProviderError("ai_response_invalid")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AiProviderError("ai_response_invalid")
    content = message.get("content")
    if not isinstance(content, str):
        raise AiProviderError("ai_response_invalid")
    return _strip_thinking_container(content)


def extract_round(
    provider: AiProvider, response: object, adapter: ProviderAdapter | None = None
) -> tuple[str | None, list[NormalizedToolCall] | None]:
    """Split one provider fixture into (final_content, tool_calls).

    M5.7 Wave C1: when the provider asks for tool calls, this returns the
    sanitized tool-call list (id/name/raw arguments) and the (nullable)
    accompanying content; the caller executes the bounded whitelist tools and
    sends the sanitized results back on the same non-streaming chain. When
    the provider returns a final answer, ``tool_calls`` is None and
    ``final_content`` is the strict final text (hidden reasoning containers
    are still stripped and never returned). A provider that fabricates tool
    calls while offering none is a malformed response.
    """
    adapter = adapter or get_provider(provider)
    if adapter.protocol == "anthropic":
        return _extract_anthropic_round(response)
    if adapter.protocol == "gemini":
        return _extract_gemini_round(response)
    get_provider(provider)  # validates and documents the selected thin adapter
    if not isinstance(response, dict):
        raise AiProviderError("ai_response_invalid")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AiProviderError("ai_response_invalid")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AiProviderError("ai_response_invalid")
    raw_tool_calls = message.get("tool_calls")
    if raw_tool_calls is not None:
        if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
            raise AiProviderError("ai_response_invalid")
        tool_calls: list[NormalizedToolCall] = []
        for call in raw_tool_calls:
            if not isinstance(call, dict):
                raise AiProviderError("ai_response_invalid")
            call_id = call.get("id")
            function = call.get("function")
            if not isinstance(call_id, str) or not call_id.strip():
                raise AiProviderError("ai_response_invalid")
            if not isinstance(function, dict):
                raise AiProviderError("ai_response_invalid")
            name = function.get("name")
            arguments = function.get("arguments", "")
            if not isinstance(name, str) or not name.strip():
                raise AiProviderError("ai_response_invalid")
            if not isinstance(arguments, str):
                raise AiProviderError("ai_response_invalid")
            tool_calls.append(NormalizedToolCall(id=call_id, name=name, arguments=arguments))
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise AiProviderError("ai_response_invalid")
        return content, tool_calls
    if choice.get("finish_reason") != "stop":
        raise AiProviderError("ai_response_invalid")
    content = message.get("content")
    if not isinstance(content, str):
        raise AiProviderError("ai_response_invalid")
    return _strip_thinking_container(content), None


def chat(
    setting: AiSettingDraft,
    api_key: str | None,
    messages: list[JsonObject],
    *,
    structured: bool,
    image_input: bool = False,
    adapter: ProviderAdapter | None = None,
) -> str:
    adapter = adapter or get_provider(setting.provider)
    payload = _build_protocol_payload(setting, messages, structured=structured, adapter=adapter)
    endpoint = (
        _endpoint(setting.base_url, "/v1/messages")
        if adapter.protocol == "anthropic"
        else (
            _endpoint(
                setting.base_url,
                f"/v1beta/models/{url_parse.quote(setting.model, safe='')}:generateContent",
            )
            if adapter.protocol == "gemini"
            else _endpoint(setting.base_url, "/v1/chat/completions")
        )
    )
    response = _request_json(
        "POST",
        endpoint,
        _protocol_headers(adapter, api_key),
        payload,
        not_found_code="ai_model_not_found",
        reasoning_explicit=setting.reasoning_mode != "default",
        image_input=image_input,
    )
    return extract_final_text(setting.provider, response, adapter)


def chat_assist(
    setting: AiSettingDraft,
    api_key: str | None,
    messages: list[JsonObject],
    *,
    tools: list[JsonObject] | None = None,
    image_input: bool = False,
    adapter: ProviderAdapter | None = None,
) -> tuple[str | None, list[NormalizedToolCall] | None]:
    """One non-streaming assist round; returns (final_content, tool_calls).

    M5.7 Wave C1: the whitelisted read-only ``tools`` definitions join the
    payload only when the Provider capability table explicitly supports them
    (``tools_supported``); otherwise the payload is byte-identical to the
    pre-C1 assist protocol and the provider cannot call any tool. The final
    answer is still expected as strict JSON through the same chain.
    """
    adapter = adapter or get_provider(setting.provider)
    if tools is not None and not adapter.tools_supported:  # capability guard, never assumed
        raise AiProviderError("ai_tool_unsupported")
    payload = _build_protocol_payload(
        setting, messages, structured=True, adapter=adapter, tools=tools
    )
    endpoint = (
        _endpoint(setting.base_url, "/v1/messages")
        if adapter.protocol == "anthropic"
        else (
            _endpoint(
                setting.base_url,
                f"/v1beta/models/{url_parse.quote(setting.model, safe='')}:generateContent",
            )
            if adapter.protocol == "gemini"
            else _endpoint(setting.base_url, "/v1/chat/completions")
        )
    )
    response = _request_json(
        "POST",
        endpoint,
        _protocol_headers(adapter, api_key),
        payload,
        not_found_code="ai_model_not_found",
        reasoning_explicit=setting.reasoning_mode != "default",
        image_input=image_input,
    )
    return extract_round(setting.provider, response, adapter)


def normalize_models(response: object) -> list[str]:
    """Normalize OpenAI ``data`` and common gateway ``models`` lists."""
    if not isinstance(response, dict):
        raise AiProviderError("ai_response_invalid")
    raw_models = response.get("data", response.get("models"))
    if not isinstance(raw_models, list):
        raise AiProviderError("ai_response_invalid")
    models: list[str] = []
    seen: set[str] = set()
    for item in raw_models:
        model_id: object = item.get("id") if isinstance(item, dict) else item
        if (
            not isinstance(model_id, str)
            or not model_id.strip()
            or contains_unicode_surrogate(model_id)
        ):
            raise AiProviderError("ai_response_invalid")
        normalized = model_id.strip()
        if normalized not in seen:
            seen.add(normalized)
            models.append(normalized)
    return models


def _normalize_gemini_models(response: object) -> list[str]:
    """Normalize the native Gemini ``models[].name`` discovery response."""
    if not isinstance(response, dict):
        raise AiProviderError("ai_response_invalid")
    raw_models = response.get("models")
    if not isinstance(raw_models, list):
        raise AiProviderError("ai_response_invalid")
    mapped = {
        "data": [
            {"id": item.get("name")} if isinstance(item, dict) else item for item in raw_models
        ]
    }
    return normalize_models(mapped)


def fetch_models(
    provider: AiProvider,
    base_url: str,
    api_key: str | None,
    adapter: ProviderAdapter | None = None,
) -> list[str]:
    """GET /v1/models with model-discovery semantics.

    A missing/unsupported endpoint and an incompatible response shape both
    mean "cannot auto-discover model IDs" (``ai_models_not_supported``), which
    stays distinct from an unreachable network and never marks the Provider
    itself as unusable: manual Model ID entry remains available.
    """
    adapter = adapter or get_provider(provider)
    endpoint = (
        _endpoint(base_url, "/v1beta/models")
        if adapter.protocol == "gemini"
        else _endpoint(base_url, "/v1/models")
    )
    response = _request_json(
        "GET",
        endpoint,
        _protocol_headers(adapter, api_key),
        not_found_code="ai_models_not_supported",
        model_discovery=True,
    )
    try:
        if adapter.protocol == "gemini":
            models = _normalize_gemini_models(response)
            return [model.removeprefix("models/") for model in models]
        return normalize_models(response)
    except AiProviderError:
        raise AiProviderError("ai_models_not_supported") from None
