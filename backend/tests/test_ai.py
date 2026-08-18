"""M4 AI Editor backend contract tests (all provider traffic is fake)."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib import error as url_error

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import Settings, settings
from dlr.control.ai import providers
from dlr.control.models import AdapterVersion, AiModelSetting, Execution
from dlr.control.schemas.ai import AiSettingDraft

PROVIDER_TOKEN = "provider-token-plaintext-sentinel"
BUSINESS_SECRET = "business-secret-plaintext-sentinel"


def create_credential(
    client: TestClient,
    *,
    name: str,
    credential_type: str,
    fields: dict[str, str],
) -> dict[str, Any]:
    response = client.post(
        "/api/credentials",
        json={"name": name, "type": credential_type, "fields": fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_adapter(client: TestClient, name: str, language: str = "python") -> dict[str, Any]:
    response = client.post(
        "/api/adapters",
        json={"name": name, "language": language, "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def save_version(
    client: TestClient,
    adapter_id: int,
    *,
    code: str = "def handle(context, input):\n    return input\n",
) -> dict[str, Any]:
    adapter = client.get(f"/api/adapters/{adapter_id}").json()
    workers = client.get("/api/workers").json()
    compatible = [
        worker
        for worker in workers
        if worker["status"] == "online" and adapter["language"] in worker["capabilities"]
    ]
    if not compatible:
        registered = client.post(
            "/api/workers/register",
            json={"name": f"ai-worker-{adapter_id}", "capabilities": [adapter["language"]]},
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
        assert registered.status_code == 200, registered.text
    response = client.post(
        f"/api/adapters/{adapter_id}/versions",
        json={"code": code, "requirements": "", "runtime_config": {}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def setting_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": "custom_openai_compatible",
        "base_url": "http://fake-provider.invalid",
        "model": "manual-model-id",
        "credential_id": None,
        "reasoning_mode": "default",
        "reasoning_effort": None,
    }
    payload.update(overrides)
    return payload


def valid_output(
    code: str = "def handle(context, input):\n    return input\n",
) -> dict[str, object]:
    return {
        "message": "Generated a candidate.",
        "candidate": {
            "summary": "Keep the adapter behavior",
            "code": code,
            "requirements": "",
            "runtime_config": {},
            "required_secret_keys": [],
        },
    }


def fake_chat_response(output: object, **message_extras: object) -> dict[str, object]:
    message: dict[str, object] = {
        "content": output if isinstance(output, str) else json.dumps(output),
    }
    message.update(message_extras)
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def configure(client: TestClient, **overrides: object) -> dict[str, Any]:
    response = client.put("/api/ai/settings", json=setting_payload(**overrides))
    assert response.status_code == 200, response.text
    return response.json()


def assist_body(code: str = "def handle(context, input):\n    return input\n") -> dict[str, object]:
    return {
        "message": "Explain this adapter and improve it.",
        "working_copy": {"code": code, "requirements": "", "runtime_config": {}},
        "recent_messages": [],
        "base_version_id": None,
    }


def selection_body(
    text: str = "def handle(context, input):",
    start_line: int = 1,
    end_line: int = 1,
) -> dict[str, object]:
    return {"source": "code", "text": text, "start_line": start_line, "end_line": end_line}


def log_snippet_body(
    text: str,
    start_line: int = 1,
    end_line: int = 1,
) -> dict[str, object]:
    """M5.5.13: a live-log context snippet (browser-visible masked text only)."""
    return {"source": "log", "text": text, "start_line": start_line, "end_line": end_line}


def test_ai_provider_timeout_settings_default_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DLR_AI_PROVIDER_TIMEOUT_SECONDS", raising=False)
    assert Settings().ai_provider_timeout_seconds == 180.0

    monkeypatch.setenv("DLR_AI_PROVIDER_TIMEOUT_SECONDS", "247.5")
    assert Settings().ai_provider_timeout_seconds == 247.5


@pytest.mark.parametrize("value", [10.0, 600.0])
def test_ai_provider_timeout_settings_accept_boundaries(
    monkeypatch: pytest.MonkeyPatch, value: float
) -> None:
    monkeypatch.setenv("DLR_AI_PROVIDER_TIMEOUT_SECONDS", str(value))
    assert Settings().ai_provider_timeout_seconds == value


@pytest.mark.parametrize("value", [9.99, 600.01])
def test_ai_provider_timeout_settings_reject_out_of_bounds(
    monkeypatch: pytest.MonkeyPatch, value: float
) -> None:
    monkeypatch.setenv("DLR_AI_PROVIDER_TIMEOUT_SECONDS", str(value))
    with pytest.raises(ValidationError):
        Settings()


def test_ai_setting_singleton_crud_never_echoes_api_key(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    missing = api_client.get("/api/ai/settings")
    assert missing.status_code == 200
    assert missing.json() is None

    credential = create_credential(
        api_client,
        name="ai-provider",
        credential_type="token",
        fields={"token": PROVIDER_TOKEN},
    )
    created = configure(
        api_client,
        provider="openai",
        base_url="https://api.example.com/",
        model="model-manual-v1",
        credential_id=credential["id"],
    )
    assert created["id"] == 1
    assert created["base_url"] == "https://api.example.com"
    assert created["credential_id"] == credential["id"]
    assert created["credential_name"] == "ai-provider"
    assert PROVIDER_TOKEN not in json.dumps(created)
    assert "ciphertext" not in created

    updated = configure(api_client, model="model-manual-v2")
    assert updated["id"] == 1
    assert updated["model"] == "model-manual-v2"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AiModelSetting)) == 1


def test_ai_setting_persists_one_normalized_provider_root(api_client: TestClient) -> None:
    saved = configure(
        api_client,
        provider="minimax",
        base_url="https://minimax.example.com//v1//",
    )
    assert saved["base_url"] == "https://minimax.example.com"


def test_ai_setting_credential_is_nullable_and_token_only(api_client: TestClient) -> None:
    assert configure(api_client, credential_id=None)["credential_id"] is None

    password = create_credential(
        api_client,
        name="wrong-kind",
        credential_type="password",
        fields={"username": "service-user", "password": BUSINESS_SECRET},
    )
    wrong_kind = api_client.put(
        "/api/ai/settings", json=setting_payload(credential_id=password["id"])
    )
    assert wrong_kind.status_code == 422
    assert wrong_kind.json()["detail"]["code"] == "ai_credential_invalid"
    assert BUSINESS_SECRET not in wrong_kind.text

    missing = api_client.put("/api/ai/settings", json=setting_payload(credential_id=999999))
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "ai_credential_invalid"


@pytest.mark.parametrize(
    ("provider", "effort"),
    [("openai", "xhigh"), ("deepseek", "max")],
)
def test_supported_provider_efforts_are_persisted(
    api_client: TestClient, provider: Any, effort: Any
) -> None:
    saved = configure(
        api_client,
        provider=provider,
        reasoning_mode="enabled",
        reasoning_effort=effort,
    )
    assert saved["reasoning_effort"] == effort
    assert api_client.get("/api/ai/settings").json()["reasoning_effort"] == effort


@pytest.mark.parametrize(
    ("provider", "message_extras", "content"),
    [
        ("openai", {"reasoning_content": "OPENAI-THOUGHT"}, "FINAL"),
        ("deepseek", {"reasoning_content": "DEEPSEEK-THOUGHT"}, "FINAL"),
        ("kimi", {"reasoning_content": "KIMI-THOUGHT"}, "FINAL"),
        ("minimax", {"reasoning_details": [{"text": "MINIMAX-THOUGHT"}]}, "FINAL"),
        (
            "custom_openai_compatible",
            {},
            "<think>CUSTOM-THOUGHT-1</think>\n<THINK>CUSTOM-THOUGHT-2</THINK>\nFINAL",
        ),
    ],
)
def test_provider_fixtures_normalize_final_text_without_reasoning(
    provider: Any, message_extras: dict[str, object], content: str
) -> None:
    response = fake_chat_response(content, **message_extras)
    assert providers.extract_final_text(provider, response) == "FINAL"


@pytest.mark.parametrize(
    "response",
    [
        {"choices": [{"message": {"reasoning_content": "only thought"}}]},
        {"choices": [{"message": {"content": "<think>unclosed"}}]},
    ],
)
def test_provider_rejects_ambiguous_or_missing_final_answer(response: object) -> None:
    with pytest.raises(providers.AiProviderError) as error:
        providers.extract_final_text("deepseek", response)
    assert error.value.code == "ai_response_invalid"


def test_provider_rejects_length_truncated_even_when_content_is_valid_json() -> None:
    response = fake_chat_response(valid_output())
    choices = response["choices"]
    assert isinstance(choices, list) and isinstance(choices[0], dict)
    choices[0]["finish_reason"] = "length"
    with pytest.raises(providers.AiProviderError) as error:
        providers.extract_final_text("openai", response)
    assert error.value.code == "ai_response_invalid"


@pytest.mark.parametrize("provider", list(providers.PROVIDERS))
def test_reasoning_default_sends_no_enable_disable_override(provider: Any) -> None:
    draft = AiSettingDraft(**setting_payload(provider=provider))
    payload = providers.build_chat_payload(
        draft, [{"role": "user", "content": "hello"}], structured=True
    )
    assert "thinking" not in payload
    assert "reasoning_effort" not in payload
    # MiniMax's output separation hint is not a reasoning enable/disable switch.
    assert (payload.get("reasoning_split") is True) == (provider == "minimax")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://minimax.example.com",
        "https://minimax.example.com/",
        "https://minimax.example.com/v1",
        "https://minimax.example.com/v1/",
        "https://minimax.example.com//v1//",
        "https://minimax.example.com/v1/v1",
    ],
)
def test_openai_compatible_endpoint_normalization_is_shared_for_chat_and_models(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_request(
        method: str,
        url: str,
        _headers: dict[str, str],
        _payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        calls.append((method, url))
        return fake_chat_response("OK") if method == "POST" else {"data": [{"id": "model"}]}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    draft = AiSettingDraft(**setting_payload(provider="minimax", base_url=base_url))
    assert providers.normalize_base_url(base_url) == "https://minimax.example.com"
    assert providers.chat(draft, None, [{"role": "user", "content": "OK"}], structured=False)
    assert providers.fetch_models("minimax", base_url, None) == ["model"]
    assert calls == [
        ("POST", "https://minimax.example.com/v1/chat/completions"),
        ("GET", "https://minimax.example.com/v1/models"),
    ]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
def test_openai_sends_only_supported_explicit_efforts(effort: Any) -> None:
    draft = AiSettingDraft(
        **setting_payload(provider="openai", reasoning_mode="enabled", reasoning_effort=effort)
    )
    payload = providers.build_chat_payload(draft, [], structured=False)
    assert payload["reasoning_effort"] == effort
    assert "thinking" not in payload


@pytest.mark.parametrize("effort", [None, "max"])
def test_openai_rejects_enabled_without_supported_explicit_effort(effort: Any) -> None:
    draft = AiSettingDraft(
        **setting_payload(provider="openai", reasoning_mode="enabled", reasoning_effort=effort)
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.build_chat_payload(draft, [], structured=False)
    assert error.value.code == "ai_reasoning_unsupported"


@pytest.mark.parametrize("effort", ["high", "max"])
def test_deepseek_sends_thinking_and_supported_effort_together(effort: Any) -> None:
    draft = AiSettingDraft(
        **setting_payload(provider="deepseek", reasoning_mode="enabled", reasoning_effort=effort)
    )
    payload = providers.build_chat_payload(draft, [], structured=False)
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == effort


def test_thinking_providers_map_explicit_mode_without_inventing_effort() -> None:
    deepseek = AiSettingDraft(**setting_payload(provider="deepseek", reasoning_mode="disabled"))
    assert providers.build_chat_payload(deepseek, [], structured=False)["thinking"] == {
        "type": "disabled"
    }

    kimi = AiSettingDraft(**setting_payload(provider="kimi", reasoning_mode="enabled"))
    kimi_payload = providers.build_chat_payload(kimi, [], structured=False)
    assert kimi_payload["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in kimi_payload


@pytest.mark.parametrize(
    ("provider", "mode", "effort"),
    [
        ("deepseek", "enabled", "low"),
        ("deepseek", "enabled", "xhigh"),
        ("kimi", "enabled", "high"),
        ("kimi", "enabled", "max"),
        ("minimax", "enabled", None),
        ("minimax", "default", "high"),
        ("custom_openai_compatible", "enabled", None),
        ("custom_openai_compatible", "default", "high"),
    ],
)
def test_provider_reasoning_capabilities_reject_unsupported_choices(
    provider: Any, mode: Any, effort: Any
) -> None:
    draft = AiSettingDraft(
        **setting_payload(provider=provider, reasoning_mode=mode, reasoning_effort=effort)
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.build_chat_payload(draft, [], structured=False)
    assert error.value.code == "ai_reasoning_unsupported"


def test_openai_uses_json_mode_not_an_invalid_strict_runtime_config_schema() -> None:
    draft = AiSettingDraft(**setting_payload(provider="openai"))
    payload = providers.build_chat_payload(draft, [], structured=True)
    assert payload["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(payload["response_format"])


def test_kimi_uses_supported_json_mode() -> None:
    draft = AiSettingDraft(**setting_payload(provider="kimi"))
    payload = providers.build_chat_payload(draft, [], structured=True)
    assert payload["response_format"] == {"type": "json_object"}


def test_provider_redirect_is_not_followed_or_forwarded() -> None:
    second_hop_requests: list[tuple[str | None, bytes]] = []

    class SecondHopHandler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            second_hop_requests.append((self.headers.get("Authorization"), self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._record()

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self._record()

        def log_message(self, *_: object) -> None:
            pass

    second = ThreadingHTTPServer(("127.0.0.1", 0), SecondHopHandler)
    second_port = second.server_address[1]

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            # urllib follows POST 302 as a GET and retains Authorization unless
            # redirect handling is explicitly disabled.
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{second_port}/stolen-chat-completions")
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    first = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    first_port = first.server_address[1]
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (first, second)
    ]
    for thread in threads:
        thread.start()
    try:
        with pytest.raises(providers.AiProviderError) as error:
            providers._request_json(
                "POST",
                f"http://127.0.0.1:{first_port}/v1/chat/completions",
                {
                    "Authorization": f"Bearer {PROVIDER_TOKEN}",
                    "Content-Type": "application/json",
                },
                {"working_copy": "must-not-reach-second-hop"},
                not_found_code="ai_model_not_found",
            )
        assert error.value.code == "ai_provider_unreachable"
        assert second_hop_requests == []
    finally:
        first.shutdown()
        second.shutdown()
        first.server_close()
        second.server_close()


@pytest.mark.parametrize(
    "response_body",
    [
        b'{"duplicate":1,"duplicate":2}',
        b'{"non_finite":NaN}',
        ('{"huge":' + "9" * 5000 + "}").encode(),
    ],
)
def test_provider_http_envelope_uses_strict_bounded_json_parser(
    monkeypatch: pytest.MonkeyPatch, response_body: bytes
) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self, _limit: int) -> bytes:
            return response_body

    class FakeOpener:
        def open(self, *_: object, **__: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(providers, "_NO_REDIRECT_OPENER", FakeOpener())
    with pytest.raises(providers.AiProviderError) as error:
        providers._request_json(
            "GET",
            "http://fake-provider.invalid/v1/models",
            {},
            not_found_code="ai_provider_unreachable",
        )
    assert error.value.code == "ai_response_invalid"


def test_provider_http_open_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    opened_with: list[float] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self, _limit: int) -> bytes:
            return b"{}"

    class FakeOpener:
        def open(self, _request: object, *, timeout: float) -> FakeResponse:
            opened_with.append(timeout)
            return FakeResponse()

    monkeypatch.setattr(settings, "ai_provider_timeout_seconds", 247.5)
    monkeypatch.setattr(providers, "_NO_REDIRECT_OPENER", FakeOpener())
    response = providers._request_json(
        "GET",
        "http://fake-provider.invalid/v1/models",
        {},
        not_found_code="ai_provider_unreachable",
    )
    assert response == {}
    assert opened_with == [247.5]


def test_models_refresh_normalizes_ids_and_failure_keeps_manual_model(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(api_client, model="keep-this-manual-model")

    def fake_request(*_: object, **__: object) -> object:
        return {
            "data": [
                {"id": "model-b"},
                {"id": "model-a"},
                {"id": "model-b"},
            ]
        }

    monkeypatch.setattr(providers, "_request_json", fake_request)
    refreshed = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json() == {"models": ["model-b", "model-a"]}

    def fake_failure(*_: object, **__: object) -> object:
        raise providers.AiProviderError("ai_provider_unreachable")

    monkeypatch.setattr(providers, "_request_json", fake_failure)
    failed = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert failed.status_code == 502
    assert failed.json()["detail"]["code"] == "ai_provider_unreachable"
    assert api_client.get("/api/ai/settings").json()["model"] == "keep-this-manual-model"


@pytest.mark.parametrize("provider", ["openai", "deepseek", "custom_openai_compatible"])
def test_models_refresh_standard_success_for_openai_compatible_providers(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Standard OpenAI ``data`` list normalizes for DeepSeek and compatible providers."""

    def fake_request(*_: object, **__: object) -> object:
        return {
            "data": [
                {"id": "deepseek-chat", "object": "model"},
                {"id": "deepseek-reasoner", "object": "model"},
            ]
        }

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": provider,
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"models": ["deepseek-chat", "deepseek-reasoner"]}


def test_models_refresh_empty_list_is_success_without_options(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(*_: object, **__: object) -> object:
        return {"data": []}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "deepseek",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"models": []}


def _opener_raising(error: BaseException) -> object:
    """Open a fake urlopen opener that deterministically raises one error."""

    class FakeOpener:
        def open(self, *_: object, **__: object) -> object:
            raise error

    return FakeOpener()


def _opener_mapping(url_errors: dict[str, BaseException], default_body: bytes) -> object:
    """Fake opener: raise per-URL errors, otherwise return a fixed JSON body."""

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def read(self, _limit: int) -> bytes:
            return default_body

    class FakeOpener:
        def open(self, request: object, *_: object, **__: object) -> FakeResponse:
            url = getattr(request, "full_url", "")
            error = url_errors.get(url)
            if error is not None:
                raise error
            return FakeResponse()

    return FakeOpener()


def test_models_refresh_unsupported_endpoint_has_actionable_chinese_error(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(api_client, model="keep-this-manual-model")
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_raising(
            url_error.HTTPError(
                "http://fake-provider.invalid/v1/models", 404, "Not Found", {}, None
            )
        ),
    )
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "deepseek",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "ai_models_not_supported"
    assert "无法自动获取模型列表" in detail["message"]
    assert "可手工填写模型 ID" in detail["message"]
    # The Provider is not marked unusable: the saved manual model survives.
    assert api_client.get("/api/ai/settings").json()["model"] == "keep-this-manual-model"


def test_models_refresh_incompatible_shape_is_not_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_mapping({}, json.dumps({"models": "not-a-list"}).encode()),
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.fetch_models("deepseek", "http://fake-provider.invalid", None)
    assert error.value.code == "ai_models_not_supported"


def test_models_refresh_method_not_allowed_is_not_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_raising(
            url_error.HTTPError(
                "http://fake-provider.invalid/v1/models", 405, "Method Not Allowed", {}, None
            )
        ),
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.fetch_models("deepseek", "http://fake-provider.invalid", None)
    assert error.value.code == "ai_models_not_supported"


def test_models_refresh_network_unreachable_is_distinct_from_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_raising(url_error.URLError("network unreachable")),
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.fetch_models("deepseek", "http://fake-provider.invalid", None)
    assert error.value.code == "ai_provider_unreachable"


def test_dns_resolution_failure_is_distinct_from_generic_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M5.5.3: DNS failures map to ai_provider_dns_failed, never to the
    generic transport error, so operators can follow the DNS first check."""
    wrapped = _opener_raising(
        url_error.URLError(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
    )
    monkeypatch.setattr(providers, "_NO_REDIRECT_OPENER", wrapped)
    with pytest.raises(providers.AiProviderError) as error:
        providers.fetch_models("deepseek", "http://fake-provider.invalid", None)
    assert error.value.code == "ai_provider_dns_failed"

    # A bare gaierror propagating as an OSError is classified the same way.
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_raising(socket.gaierror(socket.EAI_NONAME, "Name or service not known")),
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.fetch_models("deepseek", "http://fake-provider.invalid", None)
    assert error.value.code == "ai_provider_dns_failed"


def test_tcp_refused_is_generic_unreachable_not_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable resolver with a refused TCP connection stays in the
    generic transport category (TCP/TLS layer), never DNS."""
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_raising(url_error.URLError(ConnectionRefusedError("connection refused"))),
    )
    with pytest.raises(providers.AiProviderError) as error:
        providers.fetch_models("deepseek", "http://fake-provider.invalid", None)
    assert error.value.code == "ai_provider_unreachable"


def test_dns_failure_api_error_is_chinese_and_actionable(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_failure(*_: object, **__: object) -> object:
        raise providers.AiProviderError("ai_provider_dns_failed")

    monkeypatch.setattr(providers, "_request_json", fake_failure)
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "deepseek",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "ai_provider_dns_failed"
    assert "域名解析失败" in detail["message"]
    assert "DNS" in detail["message"]


def test_connection_test_and_models_refresh_are_independent(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Provider without a usable /v1/models still passes Test Connection."""
    configure(api_client)
    monkeypatch.setattr(
        providers,
        "_NO_REDIRECT_OPENER",
        _opener_mapping(
            {
                "http://fake-provider.invalid/v1/models": url_error.HTTPError(
                    "http://fake-provider.invalid/v1/models", 404, "Not Found", {}, None
                ),
            },
            json.dumps(fake_chat_response("OK")).encode(),
        ),
    )
    refreshed = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert refreshed.status_code == 502
    assert refreshed.json()["detail"]["code"] == "ai_models_not_supported"
    assert "可手工填写模型 ID" in refreshed.json()["detail"]["message"]

    tested = api_client.post(
        "/api/ai/settings/test",
        json=setting_payload(base_url="http://fake-provider.invalid", model="manual-model-id"),
    )
    assert tested.status_code == 200
    assert tested.json()["ok"] is True


def test_refresh_error_messages_are_chinese_and_actionable(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_failure(*_: object, **__: object) -> object:
        raise providers.AiProviderError("ai_provider_unreachable")

    monkeypatch.setattr(providers, "_request_json", fake_failure)
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "deepseek",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    assert response.status_code == 502
    message = response.json()["detail"]["message"]
    assert "无法连接模型服务" in message
    assert "TCP 连接或 TLS 握手失败" in message


def test_model_normalization_preserves_order_for_large_unique_list() -> None:
    items = [{"id": f"model-{index}"} for index in range(10_000)]
    models = providers.normalize_models({"data": [*items, items[0], items[-1]]})
    assert len(models) == 10_000
    assert models[:2] == ["model-0", "model-1"]
    assert models[-1] == "model-9999"


def test_models_refresh_rejects_provider_api_key_reflection(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = create_credential(
        api_client,
        name="models-provider-token",
        credential_type="token",
        fields={"token": PROVIDER_TOKEN},
    )

    def fake_request(*_: object, **__: object) -> object:
        return {"data": [{"id": f"echo-{PROVIDER_TOKEN}"}]}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "credential_id": credential["id"],
        },
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"
    assert PROVIDER_TOKEN not in response.text


def test_models_refresh_rejects_unicode_surrogate_model_id(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(*_: object, **__: object) -> object:
        return {"data": [{"id": "model-\ud800"}]}

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        "/api/ai/models/refresh",
        json={
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "credential_id": None,
        },
    )
    # A model list the discovery contract cannot normalize is an incompatible
    # /v1/models response, never a raw unsafe echo.
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_models_not_supported"
    assert "ud800" not in response.text.lower()


def test_connection_test_makes_minimal_model_request_without_saving(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        calls.append({"method": method, "url": url, "headers": headers, "payload": payload})
        return fake_chat_response("OK", reasoning_content="private thought")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        "/api/ai/settings/test",
        json=setting_payload(base_url="http://fake-provider.invalid/v1", model="test-model"),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "模型服务返回了可解析的最小响应"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://fake-provider.invalid/v1/chat/completions"
    assert calls[0]["payload"]["model"] == "test-model"  # type: ignore[index]
    assert api_client.get("/api/ai/settings").json() is None


@pytest.mark.parametrize(
    ("language", "contract_fragment", "working_code"),
    [
        ("python", "def handle(context, input):", "def handle(context, input):\n    return 1\n"),
        (
            "javascript",
            "export async function handle(context, input)",
            "export async function handle(context, input) { return 1; }\n",
        ),
        (
            "java",
            "public Object handle(Context context, Object input) throws Exception",
            "public class Adapter { public Object handle(Context c, Object i) { return i; } }",
        ),
    ],
)
def test_assist_prompt_uses_language_contract_and_secret_names_only(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    contract_fragment: str,
    working_code: str,
) -> None:
    adapter = create_adapter(api_client, f"prompt-{language}", language)
    version = save_version(api_client, adapter["id"], code=working_code)
    business_credential = create_credential(
        api_client,
        name=f"business-{language}",
        credential_type="password",
        fields={"username": "sensitive-user", "password": BUSINESS_SECRET},
    )
    binding = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [
                {
                    "env_key": "CMDB_PASSWORD",
                    "credential_id": business_credential["id"],
                    "field": "password",
                }
            ]
        },
    )
    assert binding.status_code == 200
    configure(api_client)
    captured: dict[str, object] = {}

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured["payload"] = payload or {}
        return fake_chat_response(
            valid_output(working_code),
            reasoning_content="provider-reasoning-sentinel",
            reasoning_details=[{"text": "provider-reasoning-details-sentinel"}],
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    request_body = assist_body(working_code)
    request_body["base_version_id"] = version["id"]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=request_body)
    assert response.status_code == 200, response.text
    assert response.json()["candidate"]["code"] == working_code
    assert "reasoning" not in response.text

    captured_payload = captured["payload"]
    assert isinstance(captured_payload, dict)
    messages = captured_payload["messages"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    system_prompt = messages[0]["content"]
    assert isinstance(system_prompt, str)
    assert contract_fragment in system_prompt
    encoded_code = json.dumps(working_code, ensure_ascii=False)[1:-1]
    assert encoded_code in system_prompt
    assert "CMDB_PASSWORD" in system_prompt
    assert f'"id": {version["id"]}' in system_prompt
    assert BUSINESS_SECRET not in system_prompt
    assert "sensitive-user" not in system_prompt


def test_assist_prompt_carries_exact_selection_snapshot_without_secret_values(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5.5.13: exact code and masked-log snippets (text, source and line
    range) reach the Provider as an ordered structured block, while Credential
    truth still never joins the Prompt."""
    adapter = create_adapter(api_client, "selection-context")
    version = save_version(api_client, adapter["id"])
    business_credential = create_credential(
        api_client,
        name="selection-business",
        credential_type="password",
        fields={"username": "selection-user", "password": BUSINESS_SECRET},
    )
    binding = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [
                {
                    "env_key": "SELECTION_SECRET",
                    "credential_id": business_credential["id"],
                    "field": "password",
                }
            ]
        },
    )
    assert binding.status_code == 200
    configure(api_client)
    captured: dict[str, object] = {}

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured["payload"] = payload or {}
        return fake_chat_response(
            valid_output(),
            reasoning_content="provider-reasoning-sentinel",
            reasoning_details=[{"text": "provider-reasoning-details-sentinel"}],
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    # 代码选区刻意包含前导缩进与行尾换行，且**不是** Working Copy 的子串，
    # 确保下面的断言只能由 snippet 自身命中（防止巧合通过）；日志 snippet
    # 使用浏览器可见的已脱敏文本（含统一日志时间前缀与 [REDACTED] 哨兵）。
    selected = "    items = input.get('items', [])  # exact selection\n"
    log_text = (
        "[2026-08-17 10:21:03] [ERROR] token [REDACTED] failed\n[2026-08-17 10:21:08] retry ok\n"
    )
    request_body = assist_body()
    request_body["base_version_id"] = version["id"]
    request_body["context_snippets"] = [
        selection_body(text=selected, start_line=2, end_line=3),
        log_snippet_body(text=log_text, start_line=10, end_line=11),
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=request_body)
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is not None
    # Reasoning from the Provider never reaches the browser, with or without
    # the context snippets.
    assert "reasoning" not in response.text
    assert "provider-reasoning-sentinel" not in response.text

    captured_payload = captured["payload"]
    assert isinstance(captured_payload, dict)
    messages = captured_payload["messages"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    system_prompt = messages[0]["content"]
    assert isinstance(system_prompt, str)
    # Both snippets travel verbatim (leading indentation and trailing newline
    # kept for code; masked text kept for logs), in added order, with their
    # sources and line ranges. The prompt embeds the context JSON, so newlines
    # appear in JSON-escaped form.
    encoded_selection = json.dumps(selected, ensure_ascii=False)[1:-1]
    assert encoded_selection in system_prompt
    assert "\\n" in encoded_selection
    encoded_log = json.dumps(log_text, ensure_ascii=False)[1:-1]
    assert encoded_log in system_prompt
    assert '"source": "code"' in system_prompt
    assert '"source": "log"' in system_prompt
    assert '"start_line": 2' in system_prompt
    assert '"end_line": 3' in system_prompt
    assert '"start_line": 10' in system_prompt
    assert '"end_line": 11' in system_prompt
    # The binding name is present; the Credential truth is not. Log snippets
    # carry only masked browser-visible text, never raw Secret values.
    assert "SELECTION_SECRET" in system_prompt
    assert BUSINESS_SECRET not in system_prompt
    assert "selection-user" not in system_prompt
    assert "[REDACTED]" in system_prompt


@pytest.mark.parametrize(
    ("overrides", "echo_sentinel"),
    [
        # 空白文本：仅断言稳定错误码（空白本身无法作为回显哨兵）。
        ({"text": "   "}, None),
        ({"start_line": 0}, "0"),
        ({"end_line": -424243}, "-424243"),
        ({"start_line": 424244, "end_line": 424243}, "424244"),
        ({"start_line": -424245}, "-424245"),
        ({"start_line": "STRING_ECHO_SENTINEL"}, "STRING_ECHO_SENTINEL"),
        # M5.5.13：超过长度上限的 snippet 文本同样稳定 422 且不回显。
        ({"text": "x" * 50_001}, "x" * 20),
    ],
)
def test_assist_rejects_invalid_selection_context_without_echo(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    echo_sentinel: str | None,
) -> None:
    adapter = create_adapter(api_client, "invalid-selection")
    configure(api_client)
    body = assist_body()
    body["context_snippets"] = [{**selection_body(), **overrides}]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_request_invalid"
    # 稳定错误文案是固定字符串；真正要证明的是非法原值本身不被回显。
    if echo_sentinel is not None:
        assert echo_sentinel not in response.text


def test_assist_rejects_invalid_snippet_sources_and_excessive_snippets_without_echo(
    api_client: TestClient,
) -> None:
    """M5.5.13: unknown snippet sources and over-limit snippet lists are
    rejected with the stable code and never echo the offending value."""
    adapter = create_adapter(api_client, "invalid-snippets")

    bad_source = assist_body()
    bad_source["context_snippets"] = [
        {"source": "raw_log", "text": "SOURCE_ECHO_SENTINEL", "start_line": 1, "end_line": 1}
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=bad_source)
    assert response.status_code == 422
    # Literal enum failures surface as FastAPI's standard validation detail
    # (a list); both shapes must never echo the offending values.
    assert "SOURCE_ECHO_SENTINEL" not in response.text
    assert "raw_log" not in response.text

    too_many = assist_body()
    too_many["context_snippets"] = [
        selection_body(text=f"line-{index}", start_line=index, end_line=index)
        for index in range(1, 22)
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=too_many)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_request_invalid"
    assert "line-21" not in response.text


def test_assist_selection_does_not_persist_or_log_selected_code(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M5.5.13: selected code and masked log text never land in the database
    or normal logs."""
    adapter = create_adapter(api_client, "selection-isolation")
    version = save_version(api_client, adapter["id"])
    configure(api_client)
    selected_sentinel = "SELECTION_SENTINEL_MUST_NOT_PERSIST"
    log_sentinel = "LOG_SENTINEL_MUST_NOT_PERSIST"

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(valid_output())

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body()
    body["base_version_id"] = version["id"]
    body["context_snippets"] = [
        selection_body(text=selected_sentinel, start_line=1, end_line=1),
        log_snippet_body(text=log_sentinel, start_line=1, end_line=1),
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text

    with session_factory() as session:
        versions = session.scalar(select(func.count()).select_from(AdapterVersion))
        executions = session.scalar(select(func.count()).select_from(Execution))
        assert versions is not None and executions is not None
        assert (versions, executions) == (1, 0)
        stored = session.execute(
            select(AdapterVersion.code).where(AdapterVersion.id == version["id"])
        ).scalar_one()
    assert selected_sentinel not in stored
    assert log_sentinel not in stored
    assert selected_sentinel not in "".join(record.message for record in caplog.records)
    assert log_sentinel not in "".join(record.message for record in caplog.records)
    assert selected_sentinel not in response.text
    assert log_sentinel not in response.text


@pytest.mark.parametrize(
    "provider_output",
    [
        "Here is the result:\n```json\n{}\n```",
        {"message": "missing candidate"},
        {"message": "bad type", "candidate": {"summary": "x", "code": 123}},
        {
            "message": "reasoning mixed into response",
            "candidate": None,
            "reasoning": "must never reach browser",
        },
        {
            "message": "forbidden lifecycle field",
            "candidate": {
                **valid_output()["candidate"],  # type: ignore[dict-item]
                "adapter_type": "webhook",
            },
        },
        {
            "message": "blank candidate code",
            "candidate": {**valid_output()["candidate"], "code": "   "},  # type: ignore[dict-item]
        },
    ],
)
def test_assist_rejects_every_invalid_candidate_shape(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    provider_output: object,
) -> None:
    adapter = create_adapter(api_client, "invalid-ai-output")
    configure(api_client)

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(provider_output)

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"
    assert "reasoning mixed" not in response.text


def test_assist_can_answer_without_candidate(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "explanation-only")
    configure(api_client)

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response({"message": "The adapter returns its input.", "candidate": None})

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200
    assert response.json()["candidate"] is None


def test_assist_accepts_candidate_code_containing_literal_think_tags(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "literal-think-code")
    configure(api_client)
    code = 'def handle(context, input):\n    return "<think>literal</think>"\n'

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(valid_output(code))

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    assert response.json()["candidate"]["code"] == code


@pytest.mark.parametrize("leak_field", ["message", "summary", "code"])
def test_assist_rejects_provider_api_key_reflection_anywhere_visible(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    leak_field: str,
) -> None:
    adapter = create_adapter(api_client, f"reflected-token-{leak_field}")
    credential = create_credential(
        api_client,
        name="reflected-provider-token",
        credential_type="token",
        fields={"token": PROVIDER_TOKEN},
    )
    configure(api_client, credential_id=credential["id"])
    output = valid_output()
    if leak_field == "message":
        output["message"] = f"echo {PROVIDER_TOKEN} here"
    else:
        candidate = output["candidate"]
        assert isinstance(candidate, dict)
        candidate[leak_field] = f"nested echo {PROVIDER_TOKEN} here"

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(output)

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"
    assert PROVIDER_TOKEN not in response.text


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_assist_rejects_non_finite_provider_runtime_config(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    adapter = create_adapter(api_client, f"provider-non-finite-{constant.replace('-', 'neg')}")
    configure(api_client)
    raw = (
        '{"message":"x","candidate":{"summary":"x","code":"valid",'
        '"requirements":"","runtime_config":{"value":'
        f"{constant}"
        '},"required_secret_keys":[]}}'
    )

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(raw)

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"


def test_assist_rejects_duplicate_provider_json_keys(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "provider-duplicate-json-key")
    configure(api_client)
    raw = (
        '{"message":"x","candidate":{"summary":"x","code":"valid",'
        '"requirements":"","runtime_config":{"same":1,"same":2},'
        '"required_secret_keys":[]}}'
    )

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(raw)

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"


def test_assist_rejects_lone_unicode_surrogate_in_provider_output(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "provider-lone-surrogate")
    configure(api_client)

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response('{"message":"\\ud800","candidate":null}')

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"


@pytest.mark.parametrize(
    "raw_candidate",
    [
        # Python 3.11+ caps decimal integer conversion and raises ValueError.
        (
            '{"message":"x","candidate":{"summary":"x","code":"valid",'
            '"requirements":"","runtime_config":{"huge":'
            + "9" * 5000
            + '},"required_secret_keys":[]}}'
        ),
        # Parsing or validation of adversarial nesting must remain a stable error.
        ("[" * 2000 + "0" + "]" * 2000),
    ],
)
def test_assist_maps_oversized_or_deep_provider_json_to_invalid(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    raw_candidate: str,
) -> None:
    adapter = create_adapter(api_client, f"provider-parser-limit-{len(raw_candidate)}")
    configure(api_client)

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(raw_candidate)

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "ai_response_invalid"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_assist_request_rejects_non_finite_runtime_config(
    api_client: TestClient, constant: str
) -> None:
    adapter = create_adapter(api_client, f"request-non-finite-{constant.replace('-', 'neg')}")
    body = (
        '{"message":"x","working_copy":{"code":"valid","requirements":"",'
        f'"runtime_config":{{"value":{constant}}}'
        '},"recent_messages":[]}'
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_working_copy_invalid"
    assert constant not in response.text


@pytest.mark.parametrize(
    "target",
    [
        "message",
        "working_copy.code",
        "working_copy.requirements",
        "working_copy.runtime_config.key",
        "working_copy.runtime_config.value",
        "recent_messages.content",
        "context_snippets.text",
    ],
)
def test_assist_request_rejects_unicode_surrogates_without_echo(
    api_client: TestClient, target: str
) -> None:
    adapter = create_adapter(api_client, f"request-surrogate-{target.replace('.', '-')}")
    payload = assist_body()
    payload["recent_messages"] = [{"role": "user", "content": "safe"}]
    working_copy = payload["working_copy"]
    assert isinstance(working_copy, dict)
    if target == "message":
        payload["message"] = "invalid-\ud800"
    elif target == "working_copy.code":
        working_copy["code"] = "invalid-\ud800"
    elif target == "working_copy.requirements":
        working_copy["requirements"] = "invalid-\ud800"
    elif target == "working_copy.runtime_config.key":
        working_copy["runtime_config"] = {"invalid-\ud800": "value"}
    elif target == "working_copy.runtime_config.value":
        working_copy["runtime_config"] = {"key": ["invalid-\ud800"]}
    elif target == "recent_messages.content":
        payload["recent_messages"] = [{"role": "user", "content": "invalid-\ud800"}]
    else:
        payload["context_snippets"] = [
            selection_body(text="invalid-\ud800", start_line=1, end_line=1)
        ]

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_request_invalid"
    assert "ud800" not in response.text.lower()


def test_assist_does_not_create_or_change_lifecycle_facts(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "lifecycle-isolation")
    version = save_version(api_client, adapter["id"])
    configure(api_client)
    before = api_client.get(f"/api/adapters/{adapter['id']}").json()

    def counts() -> tuple[int, int]:
        with session_factory() as session:
            versions = session.scalar(select(func.count()).select_from(AdapterVersion))
            executions = session.scalar(select(func.count()).select_from(Execution))
            assert versions is not None and executions is not None
            return versions, executions

    before_counts = counts()

    def fake_request(*_: object, **__: object) -> object:
        return fake_chat_response(valid_output())

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    after = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert counts() == before_counts == (1, 0)
    assert after["latest_version_id"] == before["latest_version_id"] == version["id"]
    assert after["adapter_type"] == before["adapter_type"]
    assert after["runtime_worker_id"] == before["runtime_worker_id"]
    assert after["runtime_locked"] == before["runtime_locked"]


def test_assist_requires_configuration(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, "not-configured")
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ai_not_configured"


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("ai_provider_unreachable", 502),
        ("ai_provider_dns_failed", 502),
        ("ai_auth_failed", 502),
        ("ai_model_not_found", 502),
        ("ai_timeout", 504),
        ("ai_response_invalid", 502),
    ],
)
def test_assist_provider_failures_have_stable_sanitized_codes(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    status_code: int,
) -> None:
    adapter = create_adapter(api_client, f"provider-error-{code}")
    configure(api_client)

    def fake_chat_assist(*_: object, **__: object) -> tuple[str | None, list[object] | None]:
        raise providers.AiProviderError(code)

    # M5.7 Wave C1: the assist protocol now runs through chat_assist; the
    # behavioral contract (stable sanitized codes, no secret reflection)
    # stays identical.
    monkeypatch.setattr(providers, "chat_assist", fake_chat_assist)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code
    assert PROVIDER_TOKEN not in response.text
    assert BUSINESS_SECRET not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        setting_payload(provider="openai", reasoning_mode="enabled"),
        setting_payload(provider="openai", reasoning_mode="enabled", reasoning_effort="max"),
        setting_payload(provider="deepseek", reasoning_mode="enabled", reasoning_effort="xhigh"),
        setting_payload(provider="kimi", reasoning_mode="enabled", reasoning_effort="high"),
        setting_payload(provider="minimax", reasoning_mode="enabled"),
        setting_payload(reasoning_mode="default", reasoning_effort="high"),
    ],
)
def test_unsupported_reasoning_is_rejected_before_any_provider_call(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    called = False

    def should_not_call(*_: object, **__: object) -> object:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(providers, "_request_json", should_not_call)
    response = api_client.post("/api/ai/settings/test", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_reasoning_unsupported"
    assert called is False


def test_ai_base_url_rejects_embedded_credentials(api_client: TestClient) -> None:
    response = api_client.put(
        "/api/ai/settings",
        json=setting_payload(base_url="https://username:password@example.com"),
    )
    assert response.status_code == 422
    assert "username" not in response.text
    assert "password" not in response.text


@pytest.mark.parametrize("field", ["base_url", "model"])
def test_ai_setting_rejects_unicode_surrogates_without_echo(
    api_client: TestClient, field: str
) -> None:
    payload = setting_payload()
    payload[field] = "invalid-\ud800"
    response = api_client.put(
        "/api/ai/settings",
        content=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_request_invalid"
    assert "ud800" not in response.text.lower()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://[::1",
        "http://example.com\n.evil.invalid",
        "http://example.com:99999",
        "http://:123",
    ],
)
def test_ai_base_url_malformed_values_have_stable_sanitized_error(
    api_client: TestClient, base_url: str
) -> None:
    response = api_client.put("/api/ai/settings", json=setting_payload(base_url=base_url))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_base_url_invalid"
    assert base_url not in response.text


def test_provider_invalid_url_is_sanitized() -> None:
    with pytest.raises(providers.AiProviderError) as error:
        providers._request_json(
            "GET",
            "http://example.com/path\nInjected: value",
            {},
            not_found_code="ai_provider_unreachable",
        )
    assert error.value.code == "ai_provider_unreachable"
