"""Thin OpenAI-compatible provider adapters used by the M4 AI Editor.

This module deliberately owns only the small, known provider differences:
structured-output hints, explicit reasoning parameters, reasoning/final
separation and model-list normalization. It is not a plugin framework.
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


PROVIDERS: dict[AiProvider, ProviderAdapter] = {
    # Candidate.runtime_config is intentionally an arbitrary JSON object.
    # OpenAI strict Structured Outputs requires closed object schemas, so a
    # strict json_schema would misrepresent this contract. Use JSON mode plus
    # the explicit prompt and mandatory local strict validation instead.
    "openai": ProviderAdapter(
        "openai",
        "json_object",
        "openai_effort",
        frozenset(("low", "medium", "high", "xhigh")),
        True,
    ),
    "deepseek": ProviderAdapter(
        "deepseek",
        "json_object",
        "thinking",
        frozenset(("high", "max")),
    ),
    # Kimi exposes the OpenAI-compatible thinking switch for models that
    # support it; upstream rejection is converted to ai_reasoning_unsupported
    # without maintaining a brittle model-name allowlist.
    "kimi": ProviderAdapter("kimi", "json_object", "thinking"),
    # MiniMax can return reasoning separately when reasoning_split is true.
    "minimax": ProviderAdapter("minimax", "prompt_only", "unsupported", split_reasoning=True),
    "custom_openai_compatible": ProviderAdapter(
        "custom_openai_compatible", "prompt_only", "unsupported"
    ),
}


def get_provider(provider: AiProvider) -> ProviderAdapter:
    return PROVIDERS[provider]


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


def _apply_reasoning(payload: JsonObject, setting: AiSettingDraft) -> None:
    adapter = get_provider(setting.provider)
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
) -> JsonObject:
    """Build one non-streaming Chat Completions request."""
    adapter = get_provider(setting.provider)
    payload: JsonObject = {
        "model": setting.model,
        "messages": messages,
        "stream": False,
    }
    _apply_reasoning(payload, setting)
    if structured and adapter.structured_output == "json_object":
        payload["response_format"] = {"type": "json_object"}
    return payload


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


def extract_final_text(provider: AiProvider, response: object) -> str:
    """Normalize one provider fixture into final text only.

    ``reasoning_content`` and ``reasoning_details`` are intentionally never
    read, returned or persisted. All supported providers use the normalized
    Chat Completions ``choices[0].message.content`` for their final answer.
    """
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


def chat(
    setting: AiSettingDraft,
    api_key: str | None,
    messages: list[JsonObject],
    *,
    structured: bool,
) -> str:
    payload = build_chat_payload(setting, messages, structured=structured)
    response = _request_json(
        "POST",
        _endpoint(setting.base_url, "/v1/chat/completions"),
        _headers(api_key),
        payload,
        not_found_code="ai_model_not_found",
        reasoning_explicit=setting.reasoning_mode != "default",
    )
    return extract_final_text(setting.provider, response)


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


def fetch_models(provider: AiProvider, base_url: str, api_key: str | None) -> list[str]:
    """GET /v1/models with model-discovery semantics.

    A missing/unsupported endpoint and an incompatible response shape both
    mean "cannot auto-discover model IDs" (``ai_models_not_supported``), which
    stays distinct from an unreachable network and never marks the Provider
    itself as unusable: manual Model ID entry remains available.
    """
    response = _request_json(
        "GET",
        _endpoint(base_url, "/v1/models"),
        _headers(api_key),
        not_found_code="ai_models_not_supported",
        model_discovery=True,
    )
    try:
        return normalize_models(response)
    except AiProviderError:
        raise AiProviderError("ai_models_not_supported") from None
