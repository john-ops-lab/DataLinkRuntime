"""Run the real smoke image block against Assist API without a Compose deployment."""

import ast
import json
from pathlib import Path

import pytest

from dlr.control.ai import providers
from dlr.control.schemas.ai import AiSettingDraft
from test_ai import (
    assist_body,
    configure,
    create_adapter,
    fake_chat_response,
    setting_payload,
    valid_output,
)
from test_issue202_model_reading import media


def test_compose_smoke_images_require_explicit_profile_and_keep_unknown_rejection(
    api_client, monkeypatch
):
    smoke = Path(__file__).parents[2] / "scripts/compose-smoke.py"
    source = smoke.read_text()
    tree = ast.parse(source)
    start = source[: source.index("image_blocked = request(")].count("\n") + 1
    end = source[: source.index("# M5.7 Wave C1: the controlled")].count("\n") + 1
    nodes = [node for node in tree.body if start <= node.lineno < end]
    png = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "PNG_1PX_BASE64" for t in node.targets)
    )
    adapter = create_adapter(api_client, "issue210-smoke-image")
    initial = setting_payload(provider="custom_openai_compatible", base_url="http://fake.invalid")
    configure(api_client, **initial)
    calls = []

    def upstream(*args, **kwargs):
        payload = args[3]
        calls.append(payload)
        user_parts = next(m["content"] for m in payload["messages"] if m["role"] == "user")
        assert isinstance(user_parts, list)
        assert user_parts[-1]["type"] == "image_url"
        assert user_parts[-1]["image_url"]["url"] == "data:image/png;base64," + png
        output = valid_output()
        output["message"] = "with native image"
        return fake_chat_response(output)

    def request(method, path, payload=None, *, expected=200):
        response = api_client.request(method, "/api" + path, json=payload)
        assert response.status_code == expected, response.text
        return response.json() if response.content else None

    monkeypatch.setattr(providers, "_request_json", upstream)
    env = {
        "request": request,
        "AI_FAKE_BASE_URL": initial["base_url"],
        "ai_adapter": adapter,
        "working_copy": assist_body()["working_copy"],
        "PNG_1PX_BASE64": png,
        "setting": initial,
        "json": json,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(smoke), "exec"), env)
    assert len(calls) == 1, "both undeclared and unknown-brand images must fail before upstream"
    current = api_client.get("/api/ai/settings").json()
    assert current["provider"] == initial["provider"] and current["custom_provider_id"] is None
    assert api_client.get("/api/ai/custom-providers").json()["providers"] == []


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_legacy_default_does_not_invent_pdf_and_explicit_adapter_preserves_media(protocol):
    helper = (
        providers._anthropic_messages if protocol == "anthropic" else providers._gemini_contents
    )
    messages = [{"role": "user", "content": providers.media_content_parts((media(pdf=True),))}]
    with pytest.raises(providers.AiProviderError) as error:
        helper(messages)
    assert error.value.code == "ai_attachment_type_unsupported"
    draft = AiSettingDraft(
        provider=protocol,
        base_url="https://api.anthropic.com"
        if protocol == "anthropic"
        else "https://generativelanguage.googleapis.com",
        model="claude-opus-5-5" if protocol == "anthropic" else "gemini-2.5-pro",
    )
    resolved = providers.resolve_attachment_adapter(draft, providers.get_provider(protocol))
    _, converted = helper(messages, resolved)
    assert media(pdf=True).data_base64 in json.dumps(converted)
    assert (
        "document" in json.dumps(converted)
        if protocol == "anthropic"
        else "inlineData" in json.dumps(converted)
    )
