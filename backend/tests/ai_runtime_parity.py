"""Opt-in pytest plugin reusing legacy domain assertions through real SDKs.

Run with ``pytest -p ai_runtime_parity ...``. Only the HTTP fixture seam is
adapted: the SDK serializes and parses the actual payload, and production
strict transport / Agent / domain code execute unchanged. Real-network tests
are separate and must not load this plugin.
"""

import json
from typing import Any

import httpx2
import pytest

from dlr.common.config import settings
from dlr.control.ai import providers
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
from dlr.control.schemas.ai import AiSettingDraft

_ORIGINAL_REQUEST_JSON = providers._request_json
_ORIGINAL_CHAT_ASSIST = providers.chat_assist


class FixtureTransport(httpx2.BaseTransport):
    def __init__(self, outer: OwnedTransport) -> None:
        self.hard_deadline = outer.hard_deadline

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        timeout = request.extensions.get("timeout", {}).get("read")
        if providers._request_json is not _ORIGINAL_REQUEST_JSON:
            result = providers._request_json(
                request.method,
                str(request.url),
                request.headers,
                payload,
                not_found_code="ai_model_not_found",
                timeout_seconds=timeout,
            )
        else:
            assert providers.chat_assist is not _ORIGINAL_CHAT_ASSIST, (
                "domain fixture must supply a provider response"
            )
            setting = AiSettingDraft(
                provider="custom_openai_compatible",
                base_url="http://fixture.invalid",
                model=payload["model"],
            )
            text, calls = providers.chat_assist(
                setting,
                None,
                payload["messages"],
                tools=payload.get("tools"),
                timeout_seconds=timeout,
                hard_deadline=self.hard_deadline,
            )
            message: dict[str, Any] = {"content": text}
            if calls is not None:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in calls
                ]
            result = {
                "choices": [
                    {"message": message, "finish_reason": "tool_calls" if calls else "stop"}
                ]
            }
        return httpx2.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx2.ByteStream(json.dumps(result).encode()),
            request=request,
        )


@pytest.fixture(autouse=True)
def use_pydantic_runtime_with_domain_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_assist_runtime", "pydantic_ai")
    original = OwnedTransport.__init__

    def init(self: OwnedTransport, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.inner.close()
        self.inner = FixtureTransport(self)  # type: ignore[assignment]

    monkeypatch.setattr(OwnedTransport, "__init__", init)
