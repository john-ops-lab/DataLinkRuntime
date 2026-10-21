"""Model discovery and draft connection testing, separate from Assist iteration."""

from urllib import parse as url_parse

from dlr.control.ai.provider_config import (
    AiProviderError,
    JsonObject,
    ProviderAdapter,
    get_provider,
)
from dlr.control.schemas.ai import AiProvider, AiSettingDraft, contains_unicode_surrogate


def chat(
    setting: AiSettingDraft,
    api_key: str | None,
    messages: list[JsonObject],
    *,
    structured: bool,
    image_input: bool = False,
    adapter: ProviderAdapter | None = None,
) -> str:
    from dlr.control.ai import providers

    adapter = adapter or get_provider(setting.provider)
    payload = providers._build_protocol_payload(
        setting, messages, structured=structured, adapter=adapter
    )
    endpoint = (
        providers._endpoint(setting.base_url, "/v1/messages")
        if adapter.protocol == "anthropic"
        else providers._endpoint(
            setting.base_url,
            f"/v1beta/models/{url_parse.quote(setting.model, safe='')}:generateContent",
        )
        if adapter.protocol == "gemini"
        else providers._endpoint(setting.base_url, "/v1/chat/completions")
    )
    response = providers._request_json(
        "POST",
        endpoint,
        providers._protocol_headers(adapter, api_key),
        payload,
        not_found_code="ai_model_not_found",
        reasoning_explicit=setting.reasoning_mode != "default",
        image_input=image_input,
    )
    return providers.extract_final_text(setting.provider, response, adapter)


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
    provider: AiProvider, base_url: str, api_key: str | None, adapter: ProviderAdapter | None = None
) -> list[str]:
    """GET /v1/models with model-discovery semantics.

    A missing/unsupported endpoint and an incompatible response shape both
    mean "cannot auto-discover model IDs" (``ai_models_not_supported``), which
    stays distinct from an unreachable network and never marks the Provider
    itself as unusable: manual Model ID entry remains available.
    """
    from dlr.control.ai import providers

    adapter = adapter or get_provider(provider)
    endpoint = (
        providers._endpoint(base_url, "/v1beta/models")
        if adapter.protocol == "gemini"
        else providers._endpoint(base_url, "/v1/models")
    )
    response = providers._request_json(
        "GET",
        endpoint,
        providers._protocol_headers(adapter, api_key),
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
