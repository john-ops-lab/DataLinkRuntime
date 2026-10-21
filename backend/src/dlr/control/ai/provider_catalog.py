"""Model discovery and draft connection testing, separate from Assist iteration."""

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
    image_input: bool = False,
    adapter: ProviderAdapter | None = None,
) -> str:
    """One tools-free SDK request for a draft; never enters the Assist Agent."""
    import asyncio
    import time

    from pydantic_ai.messages import TextPart, ToolCallPart
    from pydantic_ai.models import ModelRequestParameters

    from dlr.common.config import settings
    from dlr.control.ai.provider_config import ProviderSnapshot, validate_reasoning
    from dlr.control.ai.runtime.pydantic_ai.messages import initial_messages
    from dlr.control.ai.runtime.pydantic_ai.models import model_client

    adapter = adapter or get_provider(setting.provider)
    validate_reasoning(adapter, setting.reasoning_mode, setting.reasoning_effort)
    snapshot = ProviderSnapshot.create(setting, adapter, api_key)
    deadline = time.monotonic() + settings.ai_provider_timeout_seconds

    async def request() -> str:
        async with model_client(snapshot, deadline) as client:
            answer = await client.model.request(
                initial_messages(messages),
                client.settings,
                ModelRequestParameters(output_mode="text", function_tools=[], output_tools=[]),
            )
            if any(isinstance(part, ToolCallPart) for part in answer.parts):
                raise AiProviderError("ai_response_invalid")
            return "".join(part.content for part in answer.parts if isinstance(part, TextPart))

    try:
        return asyncio.run(request())
    except AiProviderError as error:
        # Some draft tests ask for an explicit image capability error even
        # without image parts. Keep that public 400/422 mapping unchanged.
        if image_input and error.http_status in (400, 422):
            raise AiProviderError(
                "ai_attachment_image_unsupported",
                transport_category="http",
                http_status=error.http_status,
            ) from None
        raise


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
