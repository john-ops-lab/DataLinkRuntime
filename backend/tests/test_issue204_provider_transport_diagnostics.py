"""Bounded provider transport diagnostics without secrets or retry side effects."""

import errno
import io
import json
import socket
import ssl
from http import client as http_client
from urllib import error as url_error

import pytest
from fastapi import HTTPException

from dlr.control.ai import providers, tool_audit
from dlr.control.ai.diagnostics import safe_http_status
from dlr.control.schemas.ai import AiSettingDraft
from dlr.control.services import ai

PRIVATE = "PRIVATE_URL_KEY_BODY_HEADER_EXCEPTION_MUST_NOT_APPEAR"


class UnreadBody(io.BytesIO):
    def read(self, *_args):
        pytest.fail("HTTP error body must never be read")


def http_failure(status):
    body = UnreadBody(PRIVATE.encode())
    error = url_error.HTTPError(
        f"https://{PRIVATE}.invalid/path?api_key={PRIVATE}",
        status,
        PRIVATE,
        {"Authorization": PRIVATE, "X-Upstream-Secret": PRIVATE},
        body,
    )
    return error, body


def capture(monkeypatch, failure, **options):
    calls = []

    def fail(request, timeout):
        calls.append((request, timeout))
        raise failure

    monkeypatch.setattr(providers, "_bounded_http_read", fail)
    with pytest.raises(providers.AiProviderError) as caught:
        providers._request_json(
            "POST",
            f"https://{PRIVATE}.invalid/v1/chat/completions",
            {"Authorization": f"Bearer {PRIVATE}"},
            {"private_prompt": PRIVATE},
            not_found_code="ai_model_not_found",
            **options,
        )
    assert len(calls) == 1
    error = caught.value
    assert PRIVATE not in json.dumps(vars(error)) and PRIVATE not in str(error)
    return error


@pytest.mark.parametrize("status", [302, 400, 405, 409, 422, 429, 500, 502, 503])
def test_unhandled_http_is_distinct_from_network_with_safe_public_status(monkeypatch, status):
    failure, body = http_failure(status)
    error = capture(monkeypatch, failure)
    assert body.closed
    assert error.code == "ai_provider_http_error"
    assert error.transport_category == "http" and error.http_status == status
    assert error.stage == "provider_transport" and error.reason == "transport_error"
    with pytest.raises(HTTPException) as public:
        ai._raise_provider_error(error)
    assert public.value.status_code == 502
    assert public.value.detail == {
        "code": "ai_provider_http_error",
        "message": "模型服务返回 HTTP 错误，请检查服务状态或请求配置",
        "params": {"http_status": status},
    }
    assert PRIVATE not in json.dumps(public.value.detail)


@pytest.mark.parametrize(
    "status,options,code",
    [
        (401, {}, "ai_auth_failed"),
        (403, {}, "ai_auth_failed"),
        (404, {}, "ai_model_not_found"),
        (408, {}, "ai_timeout"),
        (504, {}, "ai_timeout"),
        (400, {"image_input": True}, "ai_attachment_image_unsupported"),
        (422, {"image_input": True}, "ai_attachment_image_unsupported"),
        (400, {"reasoning_explicit": True}, "ai_reasoning_unsupported"),
        (422, {"reasoning_explicit": True}, "ai_reasoning_unsupported"),
        (400, {"model_discovery": True}, "ai_models_not_supported"),
        (405, {"model_discovery": True}, "ai_models_not_supported"),
        (422, {"model_discovery": True}, "ai_models_not_supported"),
        (401, {"image_input": True}, "ai_auth_failed"),
        (404, {"image_input": True}, "ai_model_not_found"),
        (504, {"reasoning_explicit": True}, "ai_timeout"),
        (
            400,
            {"image_input": True, "reasoning_explicit": True, "model_discovery": True},
            "ai_attachment_image_unsupported",
        ),
    ],
)
def test_special_http_codes_and_precedence_keep_public_contract(monkeypatch, status, options, code):
    failure, body = http_failure(status)
    error = capture(monkeypatch, failure, **options)
    assert body.closed
    assert error.code == code and error.transport_category == "http" and error.http_status == status
    with pytest.raises(HTTPException) as public:
        ai._raise_provider_error(error)
    assert public.value.status_code == (
        504 if code == "ai_timeout" else 422 if code == "ai_reasoning_unsupported" else 502
    )
    assert public.value.detail["code"] == code and "params" not in public.value.detail


@pytest.mark.parametrize(
    "failure,category,code",
    [
        (socket.gaierror(socket.EAI_NONAME, PRIVATE), "dns", "ai_provider_dns_failed"),
        (
            url_error.URLError(socket.gaierror(socket.EAI_AGAIN, PRIVATE)),
            "dns",
            "ai_provider_dns_failed",
        ),
        (ssl.SSLError(PRIVATE), "tls", "ai_provider_unreachable"),
        (
            url_error.URLError(ssl.SSLCertVerificationError(PRIVATE)),
            "tls",
            "ai_provider_unreachable",
        ),
        (ConnectionRefusedError(PRIVATE), "connection", "ai_provider_unreachable"),
        (
            url_error.URLError(ConnectionResetError(PRIVATE)),
            "connection",
            "ai_provider_unreachable",
        ),
        (
            url_error.URLError(OSError(errno.ENETUNREACH, PRIVATE)),
            "connection",
            "ai_provider_unreachable",
        ),
        (TimeoutError(PRIVATE), "timeout", "ai_timeout"),
        (url_error.URLError(TimeoutError(PRIVATE)), "timeout", "ai_timeout"),
        (http_client.BadStatusLine(PRIVATE), "protocol", "ai_provider_unreachable"),
        (
            url_error.URLError(http_client.IncompleteRead(b"private", 3)),
            "protocol",
            "ai_provider_unreachable",
        ),
        (url_error.URLError(PRIVATE), "unknown", "ai_provider_unreachable"),
        (ValueError(PRIVATE), "unknown", "ai_provider_unreachable"),
    ],
)
def test_network_categories_use_types_not_raw_exception_text(monkeypatch, failure, category, code):
    error = capture(monkeypatch, failure)
    assert error.code == code and error.transport_category == category and error.http_status is None
    with pytest.raises(HTTPException) as public:
        ai._raise_provider_error(error)
    assert "params" not in public.value.detail and PRIVATE not in json.dumps(public.value.detail)
    if code == "ai_provider_unreachable":
        assert "TCP 连接或 TLS 握手失败" not in public.value.detail["message"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 422, 429, 500, 504])
def test_actual_http_failure_reaches_correlated_provider_audit_once(monkeypatch, status):
    failure, _body = http_failure(status)
    read_calls = []

    def read(*args):
        read_calls.append(args)
        raise failure

    monkeypatch.setattr(providers, "_bounded_http_read", read)

    def chat(*args, **kwargs):
        providers._request_json(
            "POST",
            f"https://{PRIVATE}.invalid",
            {"Authorization": PRIVATE},
            not_found_code="ai_model_not_found",
            image_input=status == 422,
        )
        pytest.fail("HTTP failure unexpectedly succeeded")

    monkeypatch.setattr(providers, "chat_assist", chat)
    records = []
    monkeypatch.setattr(tool_audit, "_emit_record", records.append)
    trail = tool_audit.AiToolAuditTrail(tool_audit.new_request_correlation(None), 1)
    state = ai._AssistToolState.create(30, correlation=trail.correlation)
    with pytest.raises(providers.AiProviderError) as caught:
        ai._call_provider_with_audit(
            AiSettingDraft(
                provider="minimax", model="MiniMax-M3", base_url="https://api.minimax.cn"
            ),
            PRIVATE,
            [{"role": "user", "content": PRIVATE}],
            tools=None,
            provider_adapter=providers.get_provider("minimax"),
            timeout_seconds=30,
            state=state,
            audit=trail,
        )
    assert len(read_calls) == len(records) == 1
    record = records[0]
    assert record["event_type"] == "provider_result" and record["outcome"] == "error"
    assert record["transport_category"] == "http" and record["http_status"] == status
    assert record["error_code"] == caught.value.code
    assert record["request_id"] == trail.correlation.request_id
    assert record["stage"] == "provider_transport" and record["reason"] == "transport_error"
    assert PRIVATE not in json.dumps(records)


@pytest.mark.parametrize(
    "category", ["http", "dns", "tls", "connection", "timeout", "protocol", "unknown"]
)
def test_audit_accepts_only_fixed_failure_category_and_integer_status(monkeypatch, category):
    records = []
    monkeypatch.setattr(tool_audit, "_emit_record", records.append)
    trail = tool_audit.AiToolAuditTrail(tool_audit.new_request_correlation(None), 1)
    trail.record_provider_result(
        outcome="error",
        duration_ms=1,
        remaining_ms=1,
        transport_category=category,
        http_status=503,
    )
    assert records[0]["transport_category"] == category
    assert records[0].get("http_status") == (503 if category == "http" else None)
    records.clear()
    trail.record_provider_result(outcome="final", duration_ms=1, remaining_ms=1)
    assert "transport_category" not in records[0] and "http_status" not in records[0]


@pytest.mark.parametrize("status", [None, True, False, 99, 600, 502.0, "502", PRIVATE])
def test_invalid_status_is_not_coerced_or_logged(monkeypatch, status):
    assert safe_http_status(status) is None
    error = providers.AiProviderError("ai_provider_http_error", http_status=status)
    assert error.http_status is None
    records = []
    monkeypatch.setattr(tool_audit, "_emit_record", records.append)
    trail = tool_audit.AiToolAuditTrail(tool_audit.new_request_correlation(None), 1)
    trail.record_provider_result(
        outcome="error",
        duration_ms=1,
        remaining_ms=1,
        transport_category="http",
        http_status=status,
    )
    assert "http_status" not in records[0] and PRIVATE not in json.dumps(records)
    records.clear()
    trail.record_provider_result(
        outcome="error",
        duration_ms=1,
        remaining_ms=1,
        transport_category=PRIVATE,
        http_status=503,
    )
    assert records[0]["transport_category"] == "unknown" and "http_status" not in records[0]
    assert PRIVATE not in json.dumps(records)


@pytest.mark.parametrize("status", [401, 429, 503])
def test_real_socket_http_error_is_classified_audited_once_and_closed(monkeypatch, status):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []
    peer_closed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            requests.append(self.path)
            body = PRIVATE.encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Private-Header", PRIVATE)
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.connection.settimeout(1)
            try:
                if self.connection.recv(1) == b"":
                    peer_closed.set()
            except (ConnectionResetError, BrokenPipeError):
                peer_closed.set()
            finally:
                self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    records = []
    monkeypatch.setattr(tool_audit, "_emit_record", records.append)

    def chat(*args, **kwargs):
        providers._request_json(
            "POST",
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            {"Authorization": PRIVATE},
            {"private_prompt": PRIVATE},
            not_found_code="ai_model_not_found",
            timeout_seconds=2,
        )
        pytest.fail("HTTP failure unexpectedly succeeded")

    monkeypatch.setattr(providers, "chat_assist", chat)
    trail = tool_audit.AiToolAuditTrail(tool_audit.new_request_correlation(None), 1)
    try:
        with pytest.raises(providers.AiProviderError) as caught:
            ai._call_provider_with_audit(
                AiSettingDraft(
                    provider="minimax",
                    model="MiniMax-M3",
                    base_url=f"http://127.0.0.1:{server.server_port}",
                ),
                PRIVATE,
                [],
                tools=None,
                provider_adapter=providers.get_provider("minimax"),
                timeout_seconds=3,
                state=ai._AssistToolState.create(3),
                audit=trail,
            )
        assert caught.value.code == (
            "ai_auth_failed" if status == 401 else "ai_provider_http_error"
        )
        assert caught.value.transport_category == "http" and caught.value.http_status == status
        assert peer_closed.wait(1), "Client did not close the actual failed HTTP connection"
        assert requests == ["/v1/chat/completions"]
        assert len(records) == 1 and records[0]["transport_category"] == "http"
        assert records[0]["http_status"] == status
        assert PRIVATE not in json.dumps(records) and PRIVATE not in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)
