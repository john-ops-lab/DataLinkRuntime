"""Draft Catalog requests use each public SDK without entering Assist output."""

from typing import Any

import pytest

from ai_runtime_support import Protocol, response, server
from test_ai import PROVIDER_TOKEN, create_credential, setting_payload


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "google"])
def test_draft_catalog_sdk_keeps_native_url_auth_and_minimal_unsaved_request(
    api_client: Any,
    protocol: Protocol,
) -> None:
    assert api_client.get("/api/ai/settings").json() is None
    credential = create_credential(
        api_client,
        name="catalog-sdk-key",
        credential_type="token",
        fields={"token": PROVIDER_TOKEN},
    )
    provider = (
        "openai" if protocol == "openai" else "anthropic" if protocol == "anthropic" else "gemini"
    )
    with server(response(protocol, "OK")) as script:
        result = api_client.post(
            "/api/ai/settings/test",
            json=setting_payload(
                provider=provider,
                base_url=script.url + "/gateway/v1",
                model="catalog-model",
                credential_id=credential["id"],
            ),
        )
        assert result.status_code == 200, result.text
        assert result.json()["ok"] is True
        assert PROVIDER_TOKEN not in result.text
        assert len(script.requests) == 1
        wire = script.requests[0]
        expected = (
            "/gateway/v1/chat/completions"
            if protocol == "openai"
            else "/gateway/v1/messages"
            if protocol == "anthropic"
            else "/gateway/v1beta/models/catalog-model:generateContent"
        )
        assert wire.path == expected
        header = (
            "authorization"
            if protocol == "openai"
            else "x-api-key"
            if protocol == "anthropic"
            else "x-goog-api-key"
        )
        assert PROVIDER_TOKEN in wire.headers[header]
        body = wire.json()
        assert not body.get("tools") and not body.get("response_format")
        generation = body.get("generationConfig", {})
        assert "responseMimeType" not in generation and "responseJsonSchema" not in generation
        assert "candidate" not in wire.body.decode() and "request_intent" not in wire.body.decode()
    assert api_client.get("/api/ai/settings").json() is None
