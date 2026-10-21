"""Real socket drips, opaque thinking continuity and late-result rejection."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ai_sdk_fixtures
from dlr.control.ai import providers, tool_audit
from dlr.control.schemas.ai import AiSettingDraft
from dlr.control.services import ai


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_absolute_deadline_closes_real_dripping_socket(phase):
    disconnected = threading.Event()
    started = threading.Event()

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            started.set()
            try:
                if phase == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", "10000")
                    self.end_headers()
                else:
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                    self.wfile.flush()
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    runner = threading.Thread(target=server.serve_forever, daemon=True)
    runner.start()
    try:
        before = time.monotonic()
        with pytest.raises(providers.AiProviderError) as error:
            providers._request_json(
                "GET",
                f"http://127.0.0.1:{server.server_port}/drip",
                {},
                not_found_code="ai_model_not_found",
                timeout_seconds=0.18,
            )
        assert error.value.code == "ai_timeout"
        assert started.is_set() and time.monotonic() - before < 0.65
        assert disconnected.wait(0.5), "deadline must close the actual peer connection"
        assert not any(t.name == "dlr-ai-http" and t.is_alive() for t in threading.enumerate())
    finally:
        server.shutdown()
        server.server_close()
        runner.join(1)


def test_thinking_tool_turn_echoes_exact_blocks_without_public_or_audit_leak(
    api_client, monkeypatch, tmp_path
):
    from dlr.common.config import settings
    from test_ai import assist_body, configure, create_adapter

    tool_audit.close_ai_tool_audit_logging()
    monkeypatch.setattr(settings, "platform_log_root", str(tmp_path))
    adapter = create_adapter(api_client, "thinking-continuation")
    configure(
        api_client,
        provider="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-opus-5-5",
    )
    blocks = [
        {"type": "thinking", "thinking": "PRIVATE_THINKING", "signature": "PRIVATE_SIGNATURE"},
        {"type": "text", "text": "Reading docs"},
        {"type": "redacted_thinking", "data": "PRIVATE_REDACTED"},
        {"type": "tool_use", "id": "docs-1", "name": "dlr_docs_list", "input": {}},
    ]
    received = []

    def request(*args, **kwargs):
        received.append(args[3])
        if len(received) == 1:
            return {"content": blocks, "stop_reason": "tool_use"}
        assistant = next(m for m in received[-1]["messages"] if m["role"] == "assistant")
        assert assistant["content"] == blocks
        return {
            "content": [
                {"type": "thinking", "thinking": "PRIVATE_FINAL", "signature": "sig"},
                {"type": "redacted_thinking", "data": "PRIVATE_FINAL_ENCRYPTED"},
                {"type": "text", "text": json.dumps({"message": "Done", "candidate": None})},
            ],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr(providers, "_request_json", request)
    try:
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert response.status_code == 200, response.text
        assert len(received) == 2 and response.json()["message"] == "Done"
        audit = tool_audit.audit_log_path().read_text()
        assert "PRIVATE_" not in response.text and "PRIVATE_" not in audit
        assert "PRIVATE_" not in repr(
            providers._extract_anthropic_round({"content": blocks, "stop_reason": "tool_use"})[1]
        )
    finally:
        tool_audit.close_ai_tool_audit_logging()


def test_late_provider_final_is_rejected_even_when_transport_is_stubbed(monkeypatch):
    state = ai._AssistToolState.create(0.03)

    def late(*args, **kwargs):
        time.sleep(0.06)
        return '{"message":"late","candidate":null}', None

    monkeypatch.setattr(ai_sdk_fixtures, "round_reply", late)
    with pytest.raises(providers.AiProviderError) as error:
        ai_sdk_fixtures.request_once(
            AiSettingDraft(
                provider="minimax", base_url="https://api.minimax.cn", model="MiniMax-M3"
            ),
            None,
            [],
            state=state,
            audit=None,
        )
    assert error.value.code == "ai_timeout"


def test_final_service_return_rejects_late_response(session_factory, monkeypatch):
    from fastapi import HTTPException

    from dlr.control.ai.runtime.contracts import RuntimeOutcome
    from dlr.control.ai.runtime.pydantic_ai.runtime import PydanticAiRuntime
    from dlr.control.schemas.ai import AiAssistRequest, AiAssistResponse
    from test_ai import assist_body

    def late(*args, **kwargs):
        time.sleep(0.04)
        return RuntimeOutcome.from_response(
            AiAssistResponse(message="late", candidate=None, provider="minimax", model="MiniMax-M3")
        )

    ai_sdk_fixtures.stub_preparation(monkeypatch)
    monkeypatch.setattr(PydanticAiRuntime, "execute", late)
    with session_factory() as session, pytest.raises(HTTPException) as error:
        ai.assist(
            session, 1, AiAssistRequest(**assist_body()), hard_deadline=time.monotonic() + 0.02
        )
    assert error.value.status_code == 504
    assert error.value.detail["code"] == "ai_timeout"


def test_absolute_deadline_closes_real_stalled_tls_handshake():
    import socketserver

    disconnected = threading.Event()
    received_hello = threading.Event()

    class StalledTLS(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(1)
            while self.request.recv(16384):
                received_hello.set()
            disconnected.set()

    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), StalledTLS) as server:
        server.daemon_threads = True
        runner = threading.Thread(target=server.serve_forever, daemon=True)
        runner.start()
        try:
            before = time.monotonic()
            with pytest.raises(providers.AiProviderError) as error:
                providers._request_json(
                    "GET",
                    f"https://127.0.0.1:{server.server_address[1]}/",
                    {},
                    not_found_code="ai_model_not_found",
                    timeout_seconds=0.18,
                )
            assert error.value.code == "ai_timeout"
            assert received_hello.is_set() and time.monotonic() - before < 0.65
            assert disconnected.wait(0.5)
            assert not any(t.name == "dlr-ai-http" and t.is_alive() for t in threading.enumerate())
        finally:
            server.shutdown()
            runner.join(1)
