"""Run the real smoke image block against Assist API without a Compose deployment."""

import ast
import base64
import json
from pathlib import Path

import pytest

import ai_sdk_fixtures
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
def test_default_pdf_is_denied_and_official_profile_preserves_sdk_media(protocol):
    from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

    from dlr.control.ai import tools
    from test_issue202_model_reading import Reader, tool_context

    default = providers.get_provider(protocol)
    assert not default.native_pdf
    reader = Reader()
    denied = tools.execute_tool_call(
        "read_attachment_page",
        '{"attachment_ref":"ref-1","page":3,"view":"pdf"}',
        None,
        context=tool_context(reader, native_pdf=default.native_pdf),
    )
    assert denied.error_code == "ai_attachment_type_unsupported" and not reader.calls
    draft = AiSettingDraft(
        provider=protocol,
        base_url="https://api.anthropic.com"
        if protocol == "anthropic"
        else "https://generativelanguage.googleapis.com",
        model="claude-opus-5-5" if protocol == "anthropic" else "gemini-2.5-pro",
    )
    resolved = providers.resolve_attachment_adapter(draft, default)
    assert resolved.native_pdf
    item = media(pdf=True)
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    [
                        BinaryContent(
                            base64.b64decode(item.data_base64), media_type=item.content_type
                        )
                    ]
                )
            ]
        )
    ]
    payload = ai_sdk_fixtures.sdk_payload(draft, [], adapter=resolved, history=history)
    if protocol == "anthropic":
        document = next(p for p in payload["messages"][0]["content"] if p["type"] == "document")
        assert document["source"]["media_type"] == "application/pdf"
        assert base64.b64decode(document["source"]["data"]) == base64.b64decode(item.data_base64)
    else:
        binary = payload["contents"][0]["parts"][0]["inlineData"]
        assert binary.get("mimeType", binary.get("mime_type")) == "application/pdf"
        assert base64.urlsafe_b64decode(binary["data"]) == base64.b64decode(item.data_base64)
