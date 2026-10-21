"""Stable Provider configuration and capabilities, without Assist protocol codecs."""

import re
from dataclasses import dataclass, field, replace
from typing import Literal
from urllib import parse as url_parse

from dlr.control.ai.diagnostics import (
    FailureReason,
    FailureStage,
    TransportFailureCategory,
    safe_http_status,
    safe_transport_category,
)
from dlr.control.schemas.ai import AiProvider, AiSettingDraft, ReasoningEffort, ReasoningMode

MAX_PROVIDER_REQUEST_BYTES = 32 * 1024 * 1024


MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024


JsonObject = dict[str, object]


StructuredOutput = Literal["json_schema", "json_object", "prompt_only"]


ReasoningStyle = Literal["openai_effort", "thinking", "unsupported"]


Protocol = Literal["openai_compatible", "anthropic", "gemini"]


class AiProviderError(Exception):
    """Stable code and bounded diagnostics; never store raw transport details."""

    def __init__(
        self,
        code: str,
        *,
        stage: FailureStage | None = None,
        reason: FailureReason | None = None,
        transport_category: TransportFailureCategory | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage or (
            "provider_envelope" if code == "ai_response_invalid" else "provider_transport"
        )
        self.reason = reason or (
            "invalid_shape" if code == "ai_response_invalid" else "transport_error"
        )
        default_category = {
            "ai_provider_http_error": "http",
            "ai_provider_dns_failed": "dns",
            "ai_timeout": "timeout",
            "ai_response_invalid": "protocol",
        }.get(code, "unknown")
        self.transport_category = safe_transport_category(transport_category or default_category)
        self.http_status = (
            safe_http_status(http_status) if self.transport_category == "http" else None
        )


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


_OPENAI_NATIVE_PDF_MODELS = frozenset(
    {
        "gpt-4o",
        "gpt-4o-2024-08-06",
        "gpt-4o-2024-11-20",
        "gpt-4o-mini",
        "gpt-4o-mini-2024-07-18",
        "gpt-4.1",
        "gpt-4.1-mini",
    }
)


_OPENAI_NATIVE_IMAGE_MODELS = _OPENAI_NATIVE_PDF_MODELS | frozenset(
    {
        "gpt-4o-2024-05-13",
        "gpt-4.1-2025-04-14",
        "gpt-4.1-mini-2025-04-14",
        "gpt-4.1-nano",
        "gpt-4.1-nano-2025-04-14",
        "gpt-4-turbo",
        "gpt-4-turbo-2024-04-09",
    }
)


_OPENAI_1M_CONTEXT_MODELS = frozenset(
    {
        "gpt-4.1",
        "gpt-4.1-2025-04-14",
        "gpt-4.1-mini",
        "gpt-4.1-mini-2025-04-14",
        "gpt-4.1-nano",
        "gpt-4.1-nano-2025-04-14",
    }
)


_ANTHROPIC_IMAGE_ONLY_WINDOWS = {
    "claude-opus-4-5": 200000,
    "claude-opus-4-5-20251101": 200000,
    "claude-sonnet-4-5": 200000,
    "claude-sonnet-4-5-20250929": 200000,
    "claude-haiku-4-5": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-6": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4-8": 1000000,
    "claude-opus-5": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-fable-5": 1000000,
}


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
            and setting.model in _OPENAI_NATIVE_IMAGE_MODELS
        ):
            image = True
            pdf = setting.model in _OPENAI_NATIVE_PDF_MODELS
            # All six official 4.1 IDs document 1,047,576 input tokens. Use a
            # conservative 1M; 4o/Turbo retain their documented 128K boundary.
            window = 1000000 if setting.model in _OPENAI_1M_CONTEXT_MODELS else 128000
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
        image = image or setting.model in _ANTHROPIC_IMAGE_ONLY_WINDOWS
        multimodal_tools = image
        window = _ANTHROPIC_IMAGE_ONLY_WINDOWS.get(setting.model, 1000000 if image else None)
    elif (
        secure
        and setting.provider == "gemini"
        and adapter.protocol == "gemini"
        and parts.hostname == "generativelanguage.googleapis.com"
        and root in {"", "/v1beta"}
    ):
        image = setting.model in {"gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"}
        pdf = setting.model in {"gemini-2.5-pro", "gemini-2.5-flash-lite"}
        window = 1048576 if image else None
    return replace(
        adapter,
        images_native=image,
        native_pdf=pdf,
        capability_source="verified_model_endpoint" if image else "unknown",
        multimodal_tool_results=multimodal_tools,
        context_window_tokens=window,
    )


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


@dataclass(frozen=True)
class ProviderSnapshot:
    """One request's validated configuration and credential truth, never ORM state."""

    draft_json: str = field(repr=False)
    adapter: ProviderAdapter
    api_key: str | None = field(repr=False)

    @classmethod
    def create(
        cls, draft: AiSettingDraft, adapter: ProviderAdapter, api_key: str | None
    ) -> "ProviderSnapshot":
        return cls(draft.model_dump_json(exclude_unset=True), adapter, api_key)

    def draft(self) -> AiSettingDraft:
        return AiSettingDraft.model_validate_json(self.draft_json)
