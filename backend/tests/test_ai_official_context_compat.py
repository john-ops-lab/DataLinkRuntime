"""Verified 4.1 context windows must not shrink attachment-free Working Copies."""

import json

import pytest

from dlr.common.config import settings
from dlr.control.ai import context_budget, providers
from dlr.control.schemas.ai import AiSettingDraft
from test_ai import assist_body, configure, create_adapter, fake_chat_response

MODELS_1M = (
    "gpt-4.1",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini",
    "gpt-4.1-mini-2025-04-14",
    "gpt-4.1-nano",
    "gpt-4.1-nano-2025-04-14",
)
LARGE_CODE = "# comment\n" * 33000 + "def handle(context, input):\n    return input\n"


@pytest.mark.parametrize("model", MODELS_1M)
def test_large_authoritative_code_uses_documented_1m_budget(monkeypatch, model):
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 500000)
    draft = AiSettingDraft(provider="openai", model=model, base_url="https://api.openai.com/v1")
    messages = [{"role": "system", "content": "Working Copy: " + LARGE_CODE}]
    result = context_budget.prepare_call(draft, messages, None)
    assert result.fits and result.omitted_materials == 0
    assert result.diagnostics.window_tokens == 1000000
    assert result.diagnostics.window_source == "verified_model"
    assert 128000 < result.diagnostics.estimated_after_tokens < 500000
    assert messages[0]["content"].endswith(LARGE_CODE)


@pytest.mark.parametrize("model", MODELS_1M)
def test_attachment_free_large_working_copy_reaches_selected_model(api_client, monkeypatch, model):
    adapter = create_adapter(api_client, "large-working-copy")
    configure(api_client, provider="openai", model=model, base_url="https://api.openai.com")
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 500000)
    sent = []

    def upstream(*args, **kwargs):
        sent.append(args[3])
        return fake_chat_response({"message": "Reviewed the code", "candidate": None})

    monkeypatch.setattr(providers, "_request_json", upstream)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body(code=LARGE_CODE)
    )
    assert response.status_code == 200, response.text
    assert len(sent) == 1 and sent[0]["model"] == model
    request = next(m["content"] for m in sent[0]["messages"] if m["role"] == "user")
    assert isinstance(request, str)
    envelope = json.loads(request[len(context_budget.REQUEST_PREFIX) :])
    assert envelope["AUTHORITATIVE_STATE_DATA"]["working_copy"]["code"] == LARGE_CODE
    assert "UNTRUSTED_REFERENCE_MATERIAL" not in envelope


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"])
def test_128k_models_do_not_inherit_4_1_context(model):
    draft = AiSettingDraft(provider="openai", model=model, base_url="https://api.openai.com")
    result = context_budget.prepare_call(draft, [{"role": "system", "content": LARGE_CODE}], None)
    assert not result.fits and result.diagnostics.window_tokens == 128000


@pytest.mark.parametrize(
    "model,url",
    [
        ("gpt-4.1-future", "https://api.openai.com"),
        ("gpt-4.1", "https://api.openai.com/proxy"),
        ("gpt-4.1", "https://relay.invalid/v1"),
    ],
)
def test_unverified_combinations_keep_the_configured_budget(monkeypatch, model, url):
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 500000)
    draft = AiSettingDraft(provider="openai", model=model, base_url=url)
    result = context_budget.prepare_call(draft, [{"role": "system", "content": LARGE_CODE}], None)
    assert result.fits and result.diagnostics.window_tokens == 500000
    assert result.diagnostics.window_source == "configured_default"
