"""M5.11 Wave D provider protocol and custom-provider boundary contracts."""

import pytest
from fastapi.testclient import TestClient

from dlr.control.ai import providers
from test_ai import configure, setting_payload


def _custom_provider_payload(name: str = "Wave D custom") -> dict[str, object]:
    return {
        "name": name,
        "protocol": "openai_compatible",
        "base_url": "https://provider.example.invalid/v1",
        "credential_id": None,
        "images_native": False,
        "files_native": False,
        "tools_supported": True,
    }


def test_gemini_model_discovery_maps_native_names_and_strips_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def fake_request(
        method: str,
        url: str,
        _headers: dict[str, str],
        _payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        requests.append((method, url))
        return {
            "models": [
                {"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro"},
                {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash"},
                {"name": "models/gemini-2.5-pro"},
            ]
        }

    monkeypatch.setattr(providers, "_request_json", fake_request)
    models = providers.fetch_models("gemini", "https://generativelanguage.googleapis.com", None)

    assert models == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert requests == [("GET", "https://generativelanguage.googleapis.com/v1beta/models")]


def test_protocol_adapters_preserve_native_message_shapes_and_tool_rounds() -> None:
    anthropic_system, anthropic_messages = providers._anthropic_messages(
        [
            {"role": "system", "content": "system contract"},
            {"role": "user", "content": "inspect this"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "I will inspect it."}],
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "dlr_docs_search",
                            "arguments": '{"query":"contract"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"matches":[]}',
            },
        ]
    )
    assert anthropic_system == "system contract"
    assert anthropic_messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will inspect it."},
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "dlr_docs_search",
                "input": {"query": "contract"},
            },
        ],
    }
    assert anthropic_messages[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "content": '{"matches":[]}',
        }
    ]

    gemini_system, gemini_messages = providers._gemini_contents(
        [
            {"role": "system", "content": "system contract"},
            {"role": "user", "content": "inspect this"},
            {
                "role": "assistant",
                "content": "I will inspect it.",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "dlr_docs_search",
                            "arguments": '{"query":"contract"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": '{"matches":[]}',
            },
        ]
    )
    assert gemini_system == {"parts": [{"text": "system contract"}]}
    assert gemini_messages[1] == {
        "role": "model",
        "parts": [
            {"text": "I will inspect it."},
            {"functionCall": {"name": "dlr_docs_search", "args": {"query": "contract"}}},
        ],
    }
    assert gemini_messages[2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "dlr_docs_search",
                    "response": {"content": '{"matches":[]}'},
                }
            }
        ],
    }

    anthropic_text, anthropic_calls = providers.extract_round(
        "anthropic",
        {
            "content": [
                {"type": "text", "text": "I will inspect it."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "dlr_docs_search",
                    "input": {"query": "contract"},
                },
            ]
        },
    )
    assert anthropic_text == "I will inspect it."
    assert anthropic_calls == [
        providers.NormalizedToolCall("call-1", "dlr_docs_search", '{"query": "contract"}')
    ]

    gemini_text, gemini_calls = providers.extract_round(
        "gemini",
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "I will inspect it."},
                            {
                                "functionCall": {
                                    "name": "dlr_docs_search",
                                    "args": {"query": "contract"},
                                }
                            },
                        ]
                    }
                }
            ]
        },
    )
    assert gemini_text == "I will inspect it."
    assert gemini_calls == [
        providers.NormalizedToolCall("gemini-call-1", "dlr_docs_search", '{"query": "contract"}')
    ]


def test_provider_catalog_and_custom_provider_crud_boundary(
    api_client: TestClient,
) -> None:
    catalog = api_client.get("/api/ai/providers")
    assert catalog.status_code == 200, catalog.text
    by_id = {item["id"]: item for item in catalog.json()["providers"]}
    assert set(by_id) == set(providers.PROVIDERS)
    assert by_id["anthropic"]["protocol"] == "anthropic"
    assert by_id["gemini"]["protocol"] == "gemini"
    assert by_id["custom_openai_compatible"]["preset"] is True

    missing = api_client.put(
        "/api/ai/custom-providers/999999",
        json=_custom_provider_payload("missing-update"),
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "ai_custom_provider_not_found"

    invalid = api_client.post(
        "/api/ai/custom-providers",
        json={**_custom_provider_payload("invalid-url"), "base_url": "ftp://example.invalid"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "ai_base_url_invalid"

    created = api_client.post("/api/ai/custom-providers", json=_custom_provider_payload())
    assert created.status_code == 200, created.text
    provider = created.json()
    assert provider["name"] == "Wave D custom"
    assert provider["credential_id"] is None
    assert provider["referenced"] is False

    duplicate = api_client.post("/api/ai/custom-providers", json=_custom_provider_payload())
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "ai_custom_provider_name_taken"

    updated = api_client.put(
        f"/api/ai/custom-providers/{provider['id']}",
        json=_custom_provider_payload("Wave D custom updated"),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Wave D custom updated"

    setting = configure(
        api_client,
        provider="custom_openai_compatible",
        custom_provider_id=provider["id"],
    )
    assert setting["custom_provider_id"] == provider["id"]
    assert api_client.get("/api/ai/custom-providers").json()["providers"][0]["referenced"] is True

    invalid_reference = api_client.put(
        "/api/ai/settings",
        json=setting_payload(provider="openai", custom_provider_id=provider["id"]),
    )
    assert invalid_reference.status_code == 422
    assert invalid_reference.json()["detail"]["code"] == "ai_custom_provider_invalid"

    missing_reference = api_client.put(
        "/api/ai/settings",
        json=setting_payload(provider="custom_openai_compatible", custom_provider_id=999999),
    )
    assert missing_reference.status_code == 404
    assert missing_reference.json()["detail"]["code"] == "ai_custom_provider_not_found"

    referenced_delete = api_client.delete(f"/api/ai/custom-providers/{provider['id']}")
    assert referenced_delete.status_code == 409
    assert referenced_delete.json()["detail"]["code"] == "ai_custom_provider_referenced"

    configure(api_client, provider="custom_openai_compatible", custom_provider_id=None)
    deleted = api_client.delete(f"/api/ai/custom-providers/{provider['id']}")
    assert deleted.status_code == 204, deleted.text
    assert api_client.get("/api/ai/custom-providers").json() == {"providers": []}
