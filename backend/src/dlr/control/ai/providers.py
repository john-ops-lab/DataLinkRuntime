"""Small, explicit protocol adapters used by the AI Editor.

OpenAI-compatible services share one adapter, while Anthropic Messages and
Google Gemini use their native request/response shapes. Provider capabilities
are declared here rather than inferred from model names or user URLs.
"""

import json
import math
import re
import socket
import ssl
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from http import client as http_client
from typing import Any, Literal, cast
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from dlr.common.config import settings
from dlr.control.ai.attachment_reading import AttachmentMedia
from dlr.control.ai.diagnostics import FailureReason, FailureStage, StrictJsonError
from dlr.control.schemas.ai import (
    AiModelOutput,
    AiProvider,
    AiSettingDraft,
    ReasoningEffort,
    ReasoningMode,
    contains_unicode_surrogate,
)

MAX_PROVIDER_REQUEST_BYTES = 32 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024

JsonObject = dict[str, object]
StructuredOutput = Literal["json_schema", "json_object", "prompt_only"]
ReasoningStyle = Literal["openai_effort", "thinking", "unsupported"]
Protocol = Literal["openai_compatible", "anthropic", "gemini"]


@dataclass(frozen=True)
class NormalizedToolCall:
    """One validated provider tool call with typed string fields."""

    id: str
    name: str
    arguments: str
    anthropic_blocks: tuple[JsonObject, ...] = field(default=(), repr=False, compare=False)


class AiProviderError(Exception):
    """Sanitized provider failure carrying only a stable public error code."""

    def __init__(
        self,
        code: str,
        *,
        stage: FailureStage | None = None,
        reason: FailureReason | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage or (
            "provider_envelope" if code == "ai_response_invalid" else "provider_transport"
        )
        self.reason = reason or (
            "invalid_shape" if code == "ai_response_invalid" else "transport_error"
        )


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


_TRANSPORT_SCOPE = threading.local()


class _TransportScope:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancelled = False
        self.sockets: list[socket.socket] = []

    def register(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.append(sock)
            if self.cancelled:
                self._close(sock)
                raise TimeoutError

    @staticmethod
    def _close(sock: socket.socket) -> None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with suppress(OSError):
            sock.close()

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            for sock in self.sockets:
                self._close(sock)


def _scoped_create_connection(*args: Any, **kwargs: Any) -> socket.socket:
    sock = socket.create_connection(*args, **kwargs)
    scope = getattr(_TRANSPORT_SCOPE, "scope", None)
    if scope is not None:
        scope.register(sock)
    return sock


class _DeadlineHTTPConnection(http_client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _scoped_create_connection

    def connect(self) -> None:
        super().connect()
        scope = getattr(_TRANSPORT_SCOPE, "scope", None)
        if scope is not None and self.sock is not None:
            scope.register(self.sock)


class _DeadlineHTTPSConnection(http_client.HTTPSConnection):
    _context: ssl.SSLContext

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _scoped_create_connection

    def connect(self) -> None:
        # Register before TLS too, so a stalled handshake is cancellable.
        http_client.HTTPConnection.connect(self)
        scope = getattr(_TRANSPORT_SCOPE, "scope", None)
        if scope is not None and self.sock is not None:
            scope.register(self.sock)
        server_hostname = getattr(self, "_tunnel_host", None) or self.host
        context = self._context
        self.sock = context.wrap_socket(
            self.sock, server_hostname=server_hostname, do_handshake_on_connect=False
        )
        if scope is not None:
            scope.register(self.sock)
        self.sock.do_handshake()


class _DeadlineHTTPHandler(url_request.HTTPHandler):
    def http_open(self, req: url_request.Request) -> http_client.HTTPResponse:
        return self.do_open(_DeadlineHTTPConnection, req)


class _DeadlineHTTPSHandler(url_request.HTTPSHandler):
    _context: ssl.SSLContext | None

    def https_open(self, req: url_request.Request) -> http_client.HTTPResponse:
        return self.do_open(_DeadlineHTTPSConnection, req, context=self._context)


_NO_REDIRECT_OPENER = url_request.build_opener(
    _NoRedirectHandler(), _DeadlineHTTPHandler(), _DeadlineHTTPSHandler()
)


def _bounded_http_read(request: url_request.Request, timeout: float) -> bytes:
    if timeout <= 0:
        raise TimeoutError
    deadline = time.monotonic() + timeout
    scope = _TransportScope()
    finished = threading.Event()
    replies: list[bytes] = []
    errors: list[Exception] = []

    def read() -> None:
        _TRANSPORT_SCOPE.scope = scope
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
                replies.append(response.read(MAX_PROVIDER_RESPONSE_BYTES + 1))
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()
            del _TRANSPORT_SCOPE.scope

    worker = threading.Thread(target=read, daemon=True, name="dlr-ai-http")
    worker.start()
    if not finished.wait(max(0.0, deadline - time.monotonic())):
        scope.cancel()  # shutdown unblocks header/body reads, including byte drips.
        worker.join(timeout=0.2)
        raise TimeoutError
    if time.monotonic() >= deadline:
        scope.cancel()
        raise TimeoutError
    if errors:
        raise errors[0]
    return replies[0]


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
    native_pdf: bool = False
    capability_source: Literal["verified_model_endpoint", "custom_declared", "unknown"] = "unknown"
    multimodal_tool_results: bool = False
    max_request_bytes: int = MAX_PROVIDER_REQUEST_BYTES
    context_window_tokens: int | None = None
    # Tool calling is enabled by default for every model/provider. A custom
    # provider may explicitly disable it when its compatible endpoint lacks
    # that capability.
    tools_supported: bool = True
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


def resolve_output_adapter(setting: AiSettingDraft, adapter: ProviderAdapter) -> ProviderAdapter:
    """Narrow documented model/endpoint combinations, never brand capability guesses.

    Custom profiles keep their explicit protocol and conservative prompt-only
    output. Upstream rejection never selects a different model or provider.
    """
    hostname = url_parse.urlsplit(setting.base_url).hostname
    if adapter.protocol != "openai_compatible":
        return adapter
    if (
        setting.provider == "openai"
        and hostname == "api.openai.com"
        and url_parse.urlsplit(setting.base_url).path.rstrip("/") in {"", "/v1"}
        and setting.model
        in {
            "gpt-4o-mini",
            "gpt-4o-mini-2024-07-18",
            "gpt-4o-2024-08-06",
        }
    ):
        return replace(adapter, structured_output="json_schema")
    if setting.provider == "kimi" and (
        (
            hostname in {"api.kimi.com", "api.kimi.ai"}
            and url_parse.urlsplit(setting.base_url).path.rstrip("/") in {"/coding", "/coding/v1"}
            and setting.model in {"k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed"}
        )
        or (
            hostname in {"api.moonshot.cn", "api.moonshot.ai"}
            and setting.model == "kimi-thinking-preview"
        )
    ):
        # Coding docs establish distinct endpoints and model IDs but do not
        # establish response_format for this combination. Conservative mode
        # is an explicit limitation, not a claim that upstream rejects it.
        return replace(adapter, structured_output="prompt_only")
    return adapter


def resolve_attachment_adapter(
    setting: AiSettingDraft, adapter: ProviderAdapter
) -> ProviderAdapter:
    """Resolve actual input support; custom file flags remain declarations only."""
    if setting.provider == "custom_openai_compatible":
        return replace(
            adapter,
            native_pdf=False,
            capability_source="custom_declared",
            multimodal_tool_results=adapter.protocol == "anthropic" and adapter.images_native,
        )
    parts = url_parse.urlsplit(setting.base_url)
    root = parts.path.rstrip("/")
    secure = parts.scheme == "https" and parts.port in {None, 443}
    image = pdf = multimodal_tools = False
    window: int | None = None
    if secure and adapter.protocol == "openai_compatible":
        if (
            setting.provider == "openai"
            and parts.hostname == "api.openai.com"
            and root in {"", "/v1"}
            and setting.model
            in {
                "gpt-4o",
                "gpt-4o-2024-08-06",
                "gpt-4o-2024-11-20",
                "gpt-4o-mini",
                "gpt-4o-mini-2024-07-18",
                "gpt-4.1",
                "gpt-4.1-mini",
            }
        ):
            image = pdf = True
            window = 128000
        elif (
            setting.provider == "minimax"
            and parts.hostname in {"api.minimax.cn", "api.minimax.io"}
            and root in {"", "/v1"}
        ):
            image = setting.model == "MiniMax-M3"
            window = 512000 if image else None
        elif (
            setting.provider == "kimi"
            and parts.hostname in {"api.kimi.com", "api.kimi.ai"}
            and root
            in {
                "/coding",
                "/coding/v1",
            }
        ):
            image = setting.model in {"k3", "k3-256k"}
            window = 262144 if image else None
    elif (
        secure
        and setting.provider == "anthropic"
        and adapter.protocol == "anthropic"
        and parts.hostname == "api.anthropic.com"
        and root in {"", "/v1"}
    ):
        image = pdf = setting.model in {
            "claude-fable-5-1",
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
        }
        multimodal_tools = image
        window = 1000000 if image else None
    elif (
        secure
        and setting.provider == "gemini"
        and adapter.protocol == "gemini"
        and parts.hostname == "generativelanguage.googleapis.com"
        and root in {"", "/v1beta"}
    ):
        image = setting.model in {"gemini-2.5-pro", "gemini-2.5-flash"}
        pdf = setting.model == "gemini-2.5-pro"
        window = 1048576 if image else None
    return replace(
        adapter,
        images_native=image,
        native_pdf=pdf,
        capability_source="verified_model_endpoint" if image else "unknown",
        multimodal_tool_results=multimodal_tools,
        context_window_tokens=window,
    )


def media_content_parts(media: tuple[AttachmentMedia, ...]) -> list[JsonObject]:
    """A provider-neutral internal part. Body bytes never join text results."""
    return [{"type": "dlr_media", "media": item} for item in media]


def _media_part(item: JsonObject, adapter: ProviderAdapter, protocol: Protocol) -> JsonObject:
    media = item.get("media")
    if not isinstance(media, AttachmentMedia):
        raise AiProviderError("ai_response_invalid", stage="provider_envelope")
    if media.content_type == "application/pdf":
        if not adapter.native_pdf:
            raise AiProviderError("ai_attachment_type_unsupported")
        if protocol == "anthropic":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": media.content_type,
                    "data": media.data_base64,
                },
            }
        if protocol == "gemini":
            return {"inlineData": {"mimeType": media.content_type, "data": media.data_base64}}
        return {
            "type": "file",
            "file": {
                "filename": "attachment.pdf",
                "file_data": f"data:application/pdf;base64,{media.data_base64}",
            },
        }
    if not adapter.images_native:
        raise AiProviderError("ai_attachment_image_unsupported")
    if protocol == "anthropic":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media.content_type,
                "data": media.data_base64,
            },
        }
    if protocol == "gemini":
        return {"inlineData": {"mimeType": media.content_type, "data": media.data_base64}}
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{media.content_type};base64,{media.data_base64}",
            "detail": "default" if adapter.provider == "minimax" else "auto",
        },
    }


def tool_result_messages(
    call_id: str,
    name: str,
    content: str,
    media: tuple[AttachmentMedia, ...],
    adapter: ProviderAdapter,
) -> tuple[JsonObject, JsonObject | None]:
    """Keep all call/results complete before supplementary visual user messages."""
    tool: JsonObject = {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
    if adapter.protocol == "openai_compatible":
        tool.pop("name")
    if not media:
        return tool, None
    provenance = json.dumps(
        {"tool_call_id": call_id, "sources": [item.source() for item in media]},
        ensure_ascii=False,
        sort_keys=True,
    )
    parts: list[JsonObject] = [
        {"type": "text", "text": "DLR_ATTACHMENT_TOOL_MATERIAL_V1\n" + provenance},
        *media_content_parts(media),
    ]
    if adapter.multimodal_tool_results:
        tool["content"] = [{"type": "text", "text": content}, *parts]
        return tool, None
    return tool, {"role": "user", "content": parts}


def code_only_output_schema() -> JsonObject:
    """Derive a closed transport schema; local parsing still accepts legacy echoes."""
    schema = AiModelOutput.model_json_schema()
    definitions = schema.get("$defs", {})
    candidate = definitions["AiCandidate"]
    for legacy in ("requirements", "runtime_config"):
        candidate["properties"].pop(legacy, None)
    definitions.pop("JsonValue", None)

    def close(value: object) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" and isinstance(value.get("properties"), dict):
                value["additionalProperties"] = False
                value["required"] = list(value["properties"])
            for item in value.values():
                close(item)
        elif isinstance(value, list):
            for item in value:
                close(item)

    close(schema)
    return schema


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
    raise StrictJsonError("non_finite_number")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate_key")
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


def encoded_payload_size(payload: JsonObject) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def assist_payload_size(
    setting: AiSettingDraft,
    messages: list[JsonObject],
    tools: list[JsonObject] | None,
    *,
    adapter: ProviderAdapter | None = None,
) -> int:
    adapter = adapter or resolve_attachment_adapter(setting, get_provider(setting.provider))
    return encoded_payload_size(
        _build_protocol_payload(setting, messages, structured=True, adapter=adapter, tools=tools)
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
    timeout_seconds: float | None = None,
) -> object:
    """Bounded JSON HTTP request with fully sanitized failure mapping."""
    if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise AiProviderError("ai_timeout")
    request_timeout = settings.ai_provider_timeout_seconds
    if timeout_seconds is not None:
        request_timeout = min(request_timeout, timeout_seconds)
    deadline = time.monotonic() + request_timeout
    try:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = url_request.Request(url, data=data, headers=headers, method=method)
        raw = _bounded_http_read(request, max(0.0, deadline - time.monotonic()))
    except url_error.HTTPError as error:
        error.close()
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
        if isinstance(error, url_error.URLError) and isinstance(error.reason, TimeoutError):
            raise AiProviderError("ai_timeout") from None
        if _is_dns_resolution_failure(error):
            raise AiProviderError("ai_provider_dns_failed") from None
        raise AiProviderError("ai_provider_unreachable") from None
    if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="response_too_large"
        )
    try:
        parsed = load_json_strict(raw)
        if time.monotonic() >= deadline:
            raise AiProviderError("ai_timeout")
        return parsed
    except StrictJsonError as error:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason=error.reason
        ) from None
    except UnicodeDecodeError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="invalid_utf8"
        ) from None
    except json.JSONDecodeError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="malformed_json"
        ) from None
    except RecursionError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="json_limit"
        ) from None
    except ValueError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="json_value_invalid"
        ) from None


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
    adapter = resolve_output_adapter(setting, adapter or get_provider(setting.provider))
    payload: JsonObject = {
        "model": setting.model,
        "messages": messages,
        "stream": False,
    }
    transport_messages: list[JsonObject] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            converted = [
                _media_part(item, adapter, "openai_compatible")
                if isinstance(item, dict) and item.get("type") == "dlr_media"
                else item
                for item in content
            ]
            transport_message = {**message, "content": converted}
            if message.get("role") == "tool":
                transport_message.pop("name", None)
            transport_messages.append(transport_message)
        else:
            transport_message = dict(message)
            if message.get("role") == "tool":
                transport_message.pop("name", None)
            transport_messages.append(transport_message)
    payload["messages"] = transport_messages
    _apply_reasoning(payload, setting, adapter)
    if structured and adapter.structured_output == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "dlr_assist",
                "strict": True,
                "schema": code_only_output_schema(),
            },
        }
    elif structured and adapter.structured_output == "json_object":
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


def _anthropic_content(content: object, adapter: ProviderAdapter) -> object:
    """Translate the DLR visible content parts into Anthropic blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    blocks: list[dict[str, object]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "dlr_media":
            blocks.append(_media_part(item, adapter, "anthropic"))
        elif item.get("type") == "text" and isinstance(item.get("text"), str):
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


def _gemini_parts(content: object, adapter: ProviderAdapter) -> list[JsonObject]:
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        return []
    parts: list[JsonObject] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "dlr_media":
            parts.append(_media_part(item, adapter, "gemini"))
        elif item.get("type") == "text" and isinstance(item.get("text"), str):
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


def _anthropic_messages(
    messages: list[JsonObject], adapter: ProviderAdapter
) -> tuple[str | None, list[JsonObject]]:
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
                            "content": _anthropic_content(message.get("content"), adapter),
                        }
                    ],
                }
            )
            continue
        continuation = message.get("_anthropic_continuation")
        if role == "assistant" and isinstance(continuation, list):
            result.append({"role": "assistant", "content": continuation})
            continue
        role_name = "assistant" if role == "assistant" else "user"
        blocks: object = _anthropic_content(message.get("content"), adapter)
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


def _gemini_contents(
    messages: list[JsonObject], adapter: ProviderAdapter
) -> tuple[JsonObject | None, list[JsonObject]]:
    system_parts: list[JsonObject] = []
    result: list[JsonObject] = []
    tool_names: dict[str, str] = {}
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.extend(_gemini_parts(message.get("content"), adapter))
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
        parts = _gemini_parts(message.get("content"), adapter)
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
            if adapter.split_reasoning:
                # MiniMax requires the full assistant reasoning to be passed
                # back between tool rounds. Keeping it inside ``content`` is
                # the provider's documented OpenAI-native compatibility path
                # and lets the common message loop preserve it unchanged.
                payload["reasoning_split"] = False
        return payload
    if adapter.protocol == "anthropic":
        anthropic_system, converted = _anthropic_messages(messages, adapter)
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
    gemini_system, converted = _gemini_contents(messages, adapter)
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
            raise AiProviderError("ai_response_invalid", reason="incomplete_thinking")
        final = final[closing + len("</think>") :].strip()
    if not final:
        raise AiProviderError("ai_response_invalid", reason="empty_content")
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
        if contains_unicode_surrogate(block):
            raise AiProviderError("ai_response_invalid", stage="unicode", reason="invalid_unicode")
        block_type = block.get("type")
        if block_type == "text":
            if not isinstance(block.get("text"), str):
                raise AiProviderError("ai_response_invalid")
            text_parts.append(block["text"])
        elif block_type == "thinking":
            if not isinstance(block.get("thinking"), str) or not isinstance(
                block.get("signature"), str
            ):
                raise AiProviderError("ai_response_invalid")
        elif block_type == "redacted_thinking":
            if not isinstance(block.get("data"), str):
                raise AiProviderError("ai_response_invalid")
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
        # Private request-local carrier: exact content/order/signatures, never UI/audit.
        blocks = tuple(response["content"])
        tool_calls[0] = replace(tool_calls[0], anthropic_blocks=blocks)
        return ("".join(text_parts) or None), tool_calls
    if response.get("stop_reason") not in (None, "end_turn"):
        raise AiProviderError("ai_response_invalid", reason="incomplete_completion")
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
        raise AiProviderError("ai_response_invalid", reason="incomplete_completion")
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
        raise AiProviderError("ai_response_invalid", reason="incomplete_completion")
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
        raise AiProviderError("ai_response_invalid", reason="incomplete_completion")
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
    timeout_seconds: float | None = None,
    hard_deadline: float | None = None,
) -> tuple[str | None, list[NormalizedToolCall] | None]:
    """One non-streaming assist round; returns (final_content, tool_calls).

    M5.7 Wave C1: the whitelisted read-only ``tools`` definitions join the
    payload only when the Provider capability table explicitly supports them
    (``tools_supported``); otherwise the payload is byte-identical to the
    pre-C1 assist protocol and the provider cannot call any tool. The final
    answer is still expected as strict JSON through the same chain.
    """
    call_deadline = time.monotonic() + (
        timeout_seconds if timeout_seconds is not None else settings.ai_provider_timeout_seconds
    )
    if hard_deadline is not None:
        call_deadline = min(call_deadline, hard_deadline)
    adapter = resolve_attachment_adapter(setting, adapter or get_provider(setting.provider))
    if tools is not None and not adapter.tools_supported:  # capability guard, never assumed
        raise AiProviderError("ai_tool_unsupported")
    payload = _build_protocol_payload(
        setting, messages, structured=True, adapter=adapter, tools=tools
    )
    if encoded_payload_size(payload) > adapter.max_request_bytes:
        raise AiProviderError("ai_context_over_budget")
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
    remaining = call_deadline - time.monotonic()
    if remaining <= 0:
        raise AiProviderError("ai_timeout")
    response = _request_json(
        "POST",
        endpoint,
        _protocol_headers(adapter, api_key),
        payload,
        not_found_code="ai_model_not_found",
        reasoning_explicit=setting.reasoning_mode != "default",
        image_input=image_input,
        timeout_seconds=remaining,
    )
    if time.monotonic() >= call_deadline:
        raise AiProviderError("ai_timeout")
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
