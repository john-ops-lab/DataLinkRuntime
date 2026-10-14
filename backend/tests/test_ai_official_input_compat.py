"""Official image input IDs must keep the existing inline Assist path usable."""

import json
from dataclasses import replace

import pytest

from dlr.control.ai import providers
from dlr.control.schemas.ai import AiSettingDraft
from test_ai import assist_body, configure, create_adapter, fake_chat_response

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aLXcAAAAASUVORK5CYII="

RESTORED_OPENAI_IDS = (
    "gpt-4o-2024-05-13",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano",
    "gpt-4.1-nano-2025-04-14",
    "gpt-4-turbo",
    "gpt-4-turbo-2024-04-09",
)
RESTORED_ANTHROPIC_IDS = (
    "claude-opus-4-5",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
)


@pytest.mark.parametrize(
    "provider,model,url",
    [
        *[("openai", model, "https://api.openai.com") for model in RESTORED_OPENAI_IDS],
        *[("anthropic", model, "https://api.anthropic.com") for model in RESTORED_ANTHROPIC_IDS],
        ("gemini", "gemini-2.5-flash-lite", "https://generativelanguage.googleapis.com"),
    ],
)
def test_documented_model_keeps_legacy_inline_pixels(api_client, monkeypatch, provider, model, url):
    adapter = create_adapter(api_client, "official-inline-image")
    configure(api_client, provider=provider, model=model, base_url=url)
    captured = []
    output = {"message": "Read the supplied pixels", "candidate": None}

    def upstream(*args, **kwargs):
        captured.append(args[3])
        if provider == "anthropic":
            return {
                "content": [{"type": "text", "text": json.dumps(output)}],
                "stop_reason": "end_turn",
            }
        if provider == "gemini":
            return {
                "candidates": [
                    {
                        "content": {"parts": [{"text": json.dumps(output)}]},
                        "finishReason": "STOP",
                    }
                ]
            }
        return fake_chat_response(output)

    monkeypatch.setattr(providers, "_request_json", upstream)
    body = assist_body()
    body["message"] = "Describe this image without changing code"
    body["attachments"] = [
        {"filename": "pixel.png", "content_type": "image/png", "data_base64": PNG}
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is None and len(captured) == 1
    if provider == "gemini":
        pixels = next(
            part["inlineData"]
            for message in captured[0]["contents"]
            for part in message["parts"]
            if "inlineData" in part
        )
        assert pixels == {"mimeType": "image/png", "data": PNG}
    elif provider == "anthropic":
        assert captured[0]["model"] == model and "response_format" not in captured[0]
        pixels = next(
            part["source"]
            for message in captured[0]["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image"
        )
        assert pixels == {"type": "base64", "media_type": "image/png", "data": PNG}
    else:
        assert captured[0]["model"] == model
        pixels = next(
            part["image_url"]
            for message in captured[0]["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        )
        assert pixels["url"] == "data:image/png;base64," + PNG
        # Image evidence must not promote an unrelated strict output contract.
        assert captured[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("model", RESTORED_OPENAI_IDS)
def test_restored_images_do_not_invent_pdf_or_strict_output(model):
    draft = AiSettingDraft(provider="openai", model=model, base_url="https://api.openai.com/v1")
    adapter = providers.resolve_output_adapter(
        draft, providers.resolve_attachment_adapter(draft, providers.get_provider("openai"))
    )
    assert adapter.images_native and not adapter.native_pdf
    assert adapter.structured_output == "json_object"


@pytest.mark.parametrize(
    "provider,model,base_url,protocol",
    [
        ("openai", "gpt-4.1-nano", "https://api.openai.com/proxy", "openai_compatible"),
        ("openai", "gpt-4.1-nano", "http://api.openai.com", "openai_compatible"),
        ("openai", "gpt-4.1-nano", "https://api.openai.com:444/v1", "openai_compatible"),
        ("openai", "gpt-4.1-nano", "https://relay.invalid/v1", "openai_compatible"),
        ("openai", "gpt-4.1-nano", "https://api.openai.com/v1", "anthropic"),
        ("openai", "gpt-4.1-nano-future", "https://api.openai.com/v1", "openai_compatible"),
        (
            "gemini",
            "gemini-2.5-flash-lite",
            "https://generativelanguage.googleapis.com/proxy",
            "gemini",
        ),
        (
            "gemini",
            "gemini-2.5-flash-lite-future",
            "https://generativelanguage.googleapis.com",
            "gemini",
        ),
    ],
)
def test_new_input_ids_still_require_exact_official_combination(
    provider, model, base_url, protocol
):
    draft = AiSettingDraft(provider=provider, model=model, base_url=base_url)
    original = replace(providers.get_provider(provider), protocol=protocol)
    adapter = providers.resolve_attachment_adapter(draft, original)
    assert not adapter.images_native and not adapter.native_pdf
    assert adapter.capability_source == "unknown"


def test_flash_lite_native_pdf_uses_documented_inline_transport():
    from test_issue202_model_reading import media

    draft = AiSettingDraft(
        provider="gemini",
        model="gemini-2.5-flash-lite",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    adapter = providers.resolve_attachment_adapter(draft, providers.get_provider("gemini"))
    assert adapter.images_native and adapter.native_pdf
    item = media(pdf=True)
    payload = providers._build_protocol_payload(
        draft,
        [{"role": "user", "content": providers.media_content_parts((item,))}],
        structured=True,
        adapter=adapter,
    )
    assert payload["contents"][0]["parts"][0]["inlineData"] == {
        "mimeType": "application/pdf",
        "data": item.data_base64,
    }


@pytest.mark.parametrize("model", RESTORED_ANTHROPIC_IDS)
def test_served_anthropic_image_ids_keep_their_verified_window(model):
    draft = AiSettingDraft(provider="anthropic", model=model, base_url="https://api.anthropic.com")
    adapter = providers.resolve_attachment_adapter(draft, providers.get_provider("anthropic"))
    assert adapter.images_native and adapter.multimodal_tool_results
    assert not adapter.native_pdf and adapter.structured_output == "prompt_only"
    assert adapter.context_window_tokens == (200000 if "-4-5" in model else 1000000)


@pytest.mark.parametrize(
    "model", ["claude-opus-4-1-20250805", "claude-sonnet-4-20250514", "claude-haiku-4-5-20990101"]
)
def test_retired_or_invented_anthropic_ids_are_not_added(model):
    draft = AiSettingDraft(provider="anthropic", model=model, base_url="https://api.anthropic.com")
    adapter = providers.resolve_attachment_adapter(draft, providers.get_provider("anthropic"))
    assert not adapter.images_native and adapter.capability_source == "unknown"
