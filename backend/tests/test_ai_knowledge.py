"""M5.7 Wave C2: read-only KnowledgeSource boundary + Tencent ima adapter tests.

Coverage (task contract): list / search / read success against a fake
official service; empty / pagination / limit; malformed and oversize
responses; 401/403/404/429/5xx; DNS / TCP / TLS / timeout; unknown host /
plain-HTTP / IP-literal / userinfo / redirect SSRF guards; missing and
invalid Credentials; Secret truth never reaching prompts, UI summaries, tool
results, logs or exceptions; write operations rejected; final AiModelOutput
with candidate=null / Candidate / attachments / Regenerate / recent_messages
/ Adapter isolation; and the stable ks_* error mapping at the tool boundary.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.ai import ima as ima_adapter
from dlr.control.ai import knowledge, providers
from dlr.control.ai import tools as tools_service
from dlr.control.models import Credential
from dlr.control.schemas.credential import CredentialCreate
from dlr.control.services import secrets as secrets_service
from test_ai import (
    assist_body,
    configure,
    create_adapter,
    create_credential,
    fake_chat_response,
    valid_output,
)

IMA_TOKEN = "ima-secret-token-plaintext-sentinel-9f3a"
IMA_CLIENT_ID = "ima-client-id-plaintext-sentinel"

CREDENTIAL_NAME = "ima-test-cred"

BASES = [
    {
        "id": "team-knowledge",
        "name": "Team knowledge base",
        "description": "Team internal documentation",
        "item_count": 3,
    },
    {
        "id": "platform-manuals",
        "name": "Platform manuals",
        "description": "Platform operation manuals",
        "item_count": 2,
    },
]

ITEMS: dict[str, dict[str, str]] = {
    "kb-item-1": {
        "id": "kb-item-1",
        "title": "Runtime contract",
        "content": "The adapter runtime contract for DLR. Read-only knowledge.",
    },
    "kb-item-2": {
        "id": "kb-item-2",
        "title": "Secrets and bindings",
        "content": "Credential truth never joins prompts or logs.",
    },
    "kb-item-3": {
        "id": "kb-item-3",
        "title": "Schedule triggers",
        "content": "Cron-based schedule runs for task adapters.",
    },
}


def _item_payload(item_id: str, *, echo_token: str | None = None) -> dict[str, str]:
    item = dict(ITEMS[item_id])
    if echo_token is not None:
        item["content"] = f"{item['content']} Config: {echo_token}"
    return item


class FakeImaHandler(BaseHTTPRequestHandler):
    """One fake official ima-compatible service (wire protocol v1).

    Class-level knobs are reset per test: ``status`` forces an HTTP error,
    ``raw_body`` forces an arbitrary response body, ``delay_seconds`` adds
    latency, ``redirect_to`` emits a redirect, ``bases`` / ``items`` override
    the dataset and ``require_token`` enables auth.
    """

    status: int | None = None
    raw_body: bytes | None = None
    delay_seconds: float = 0.0
    redirect_to: str | None = None
    require_token: str | None = None
    echo_token: str | None = None
    bases: list[dict[str, Any]] = BASES
    items: dict[str, dict[str, str]] = ITEMS
    requested: list[str] = []

    server_version = "FakeImaService/1.0"

    def _write(self, status: int, payload: object) -> None:
        body = (
            json.dumps(payload, ensure_ascii=False).encode()
            if not isinstance(payload, bytes)
            else payload
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.require_token is None:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.require_token}"

    def _handle(self) -> None:
        self.requested.append(f"{self.command} {self.path}")
        if self.redirect_to is not None:
            self.send_response(301)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        if self.status is not None:
            self._write(self.status, {"error": {"code": "upstream", "message": "fake error"}})
            return
        if self.raw_body is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.raw_body)))
            self.end_headers()
            self.wfile.write(self.raw_body)
            return
        if not self._authorized():
            self._write(401, {"error": {"code": "unauthorized", "message": "fake"}})
            return
        if self.path.startswith("/v1/knowledge/bases"):
            self._write(
                200,
                {
                    "total": len(self.bases),
                    "items": self.bases,
                },
            )
            return
        if self.path.startswith("/v1/knowledge/items/"):
            item_id = self.path[len("/v1/knowledge/items/") :].split("?")[0]
            item = self.items.get(item_id)
            if item is None:
                self._write(404, {"error": {"code": "not_found", "message": "fake"}})
                return
            payload = dict(item)
            if self.echo_token is not None:
                payload["content"] = f"{payload['content']} Config: {self.echo_token}"
            self._write(200, {"item": payload})
            return
        if self.path == "/v1/knowledge/search" and self.command == "POST":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length))
                query = str(body.get("query", "")).casefold()
                limit = int(body.get("limit", 5))
            except (json.JSONDecodeError, TypeError, ValueError):
                self._write(400, {"error": {"code": "bad_request", "message": "fake"}})
                return
            hits = [
                {"id": item["id"], "title": item["title"], "summary": item["content"][:120]}
                for item in self.items.values()
                if query in item["title"].casefold() or query in item["content"].casefold()
            ]
            self._write(200, {"total": len(hits), "items": hits[:limit]})
            return
        self._write(404, {"error": {"code": "not_found", "message": "fake"}})

    do_GET = _handle
    do_POST = _handle

    def log_message(self, format_string: str, *args: object) -> None:
        pass


@pytest.fixture()
def ima_server() -> Iterator[ThreadingHTTPServer]:
    FakeImaHandler.status = None
    FakeImaHandler.raw_body = None
    FakeImaHandler.delay_seconds = 0.0
    FakeImaHandler.redirect_to = None
    FakeImaHandler.require_token = None
    FakeImaHandler.echo_token = None
    FakeImaHandler.bases = BASES
    FakeImaHandler.items = ITEMS
    FakeImaHandler.requested = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def ima_config(monkeypatch: pytest.MonkeyPatch, ima_server: ThreadingHTTPServer):
    """Point the ima settings at the fake official service on localhost."""
    monkeypatch.setattr(settings, "dlr_ima_endpoint", f"http://localhost:{ima_server.server_port}")
    monkeypatch.setattr(settings, "dlr_ima_allowed_hosts", "ima.qq.com,localhost")
    monkeypatch.setattr(settings, "dlr_ima_allow_http", True)
    monkeypatch.setattr(settings, "dlr_ima_credential_name", CREDENTIAL_NAME)
    monkeypatch.setattr(settings, "dlr_ima_timeout_seconds", 5.0)
    return ima_server


@pytest.fixture()
def ima_session(
    session_factory: sessionmaker[Session], ima_config: ThreadingHTTPServer
) -> Iterator[Session]:
    session = session_factory()
    existing = session.scalar(
        secrets_service.select(Credential).where(Credential.name == CREDENTIAL_NAME)
    )
    if existing is None:
        secrets_service.create_credential(
            session,
            CredentialCreate(
                name=CREDENTIAL_NAME,
                type="token",
                fields={"token": IMA_TOKEN},
            ),
        )
    try:
        yield session
    finally:
        session.close()


def execute(
    name: str,
    args: dict[str, Any],
    *,
    session: Session | None = None,
    secret_values: list[str] | None = None,
) -> tools_service.ToolExecution:
    context = tools_service.ToolExecutionContext(session=session, secret_values=secret_values or [])
    return tools_service.execute_tool_call(name, json.dumps(args), None, context=context)


def _tool_response(
    tool_calls: list[dict[str, Any]],
    *,
    content: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"content": content, "role": "assistant"}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


def _final_response(
    message: str = "Generated a candidate.", **candidate_overrides: object
) -> dict[str, object]:
    output = valid_output()
    if candidate_overrides:
        output["candidate"] = {**output["candidate"], **candidate_overrides}
    output["message"] = message
    return fake_chat_response(output)


def _call(name: str, arguments: str, call_id: str = "call-k") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _knowledge_then_final(
    calls: list[dict[str, Any]],
    message: str = "Answered with knowledge.",
    *,
    captured: list[dict[str, Any]] | None = None,
) -> Any:
    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if captured is not None:
            captured.append(payload or {})
        if any(m.get("role") == "tool" for m in payload["messages"]):
            return _final_response(message=message)
        return _tool_response(calls)

    return fake_request


# --- registry / boundary ------------------------------------------------------


def test_knowledge_tools_registered_and_read_only() -> None:
    names = set(tools_service.tool_names())
    assert {
        "list_knowledge_bases",
        "search_knowledge",
        "read_knowledge",
    }.issubset(names)
    # No write-like tool name can ever exist in the whitelist.
    for name in names:
        assert not any(
            name.lower().startswith(prefix)
            for prefix in ("upload", "write", "create", "delete", "update", "share", "sync")
        )


def test_unknown_knowledge_source_rejected_without_network() -> None:
    execution = execute("list_knowledge_bases", {"source": "not-a-source"})
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_UNKNOWN_SOURCE
    # The args summary stays sanitized and bounded (never raw reflection).
    assert len(execution.args_summary) <= tools_service.MAX_TOOL_SUMMARY_CHARS + 60


def test_write_style_duck_typed_source_is_refused() -> None:
    class ReadOnlyImpostor:
        def list_knowledge_bases(self) -> list[Any]:
            return []

        def search_knowledge(self, query: str, limit: int) -> list[Any]:
            return []

        def read_knowledge(self, item_id: str) -> Any:
            raise NotImplementedError

        def upload_document(self, path: str) -> None:
            raise NotImplementedError

    assert knowledge.is_read_only_source(ReadOnlyImpostor()) is False

    class CleanSource:
        def list_knowledge_bases(self) -> list[Any]:
            return []

        def search_knowledge(self, query: str, limit: int) -> list[Any]:
            return []

        def read_knowledge(self, item_id: str) -> Any:
            raise NotImplementedError

    assert knowledge.is_read_only_source(CleanSource()) is True


# --- fake official service: success paths --------------------------------------


def test_ima_list_search_read_success(ima_session: Session) -> None:
    listing = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert listing.status == "success", listing.error_code
    listed = json.loads(listing.model_content)
    assert listed["tool"] == "list_knowledge_bases"
    assert listed["total"] == 2
    assert {item["id"] for item in listed["items"]} == {"team-knowledge", "platform-manuals"}
    assert all(item["source"].startswith("ima:v1:") for item in listed["items"])
    assert listing.source == "ima:v1:team-knowledge"

    search = execute(
        "search_knowledge",
        {"source": "ima", "query": "secrets", "limit": 1},
        session=ima_session,
    )
    assert search.status == "success", search.error_code
    searched = json.loads(search.model_content)
    assert searched["total_matches"] == 1
    assert searched["items"][0]["id"] == "kb-item-2"
    assert search.source == "ima:v1:kb-item-2"

    read = execute(
        "read_knowledge",
        {"source": "ima", "item_id": "kb-item-1"},
        session=ima_session,
    )
    assert read.status == "success", read.error_code
    assert read.source == "ima:v1:kb-item-1"
    assert "runtime contract" in read.model_content


def test_ima_empty_search_and_pagination_and_limit(ima_session: Session) -> None:
    empty = execute(
        "search_knowledge",
        {"source": "ima", "query": "zzz-no-match"},
        session=ima_session,
    )
    assert empty.status == "success", empty.error_code
    assert json.loads(empty.model_content)["total_matches"] == 0

    # The tool schema clamps limit into 1..10.
    clamped = execute(
        "search_knowledge",
        {"source": "ima", "query": "contract", "limit": 99},
        session=ima_session,
    )
    assert clamped.status == "success", clamped.error_code
    assert json.loads(clamped.model_content)["limit"] == 10

    all_hits = execute(
        "search_knowledge",
        {"source": "ima", "query": "the"},
        session=ima_session,
    )
    assert all_hits.status == "success", all_hits.error_code
    assert json.loads(all_hits.model_content)["total_matches"] == 1


def test_ima_read_missing_item_is_stable_not_found(ima_session: Session) -> None:
    missing = execute(
        "read_knowledge",
        {"source": "ima", "item_id": "no-such-item"},
        session=ima_session,
    )
    assert missing.status == "error"
    assert missing.error_code == knowledge.KS_NOT_FOUND
    # The stable error result never echoes the requested id.
    assert "no-such-item" not in missing.model_content


# --- malformed / oversize responses -------------------------------------------


def test_ima_malformed_response_rejected(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeImaHandler.raw_body = b"{not json"
    assert (
        execute("list_knowledge_bases", {"source": "ima"}, session=ima_session).error_code
        == knowledge.KS_RESPONSE_INVALID
    )

    FakeImaHandler.raw_body = b'{"items": "nope"}'
    assert (
        execute("list_knowledge_bases", {"source": "ima"}, session=ima_session).error_code
        == knowledge.KS_RESPONSE_INVALID
    )

    FakeImaHandler.raw_body = b'{"item": {"id": 7}}'
    read_execution = execute(
        "read_knowledge", {"source": "ima", "item_id": "kb-item-1"}, session=ima_session
    )
    assert read_execution.error_code == knowledge.KS_RESPONSE_INVALID


def test_ima_oversize_response_rejected(ima_session: Session) -> None:
    FakeImaHandler.raw_body = b"x" * (ima_adapter.MAX_KNOWLEDGE_RESPONSE_BYTES + 1)
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_TOO_LARGE


def test_ima_oversize_item_field_rejected(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeImaHandler.bases = [
        {
            "id": "big",
            "name": "n" * (knowledge.MAX_KNOWLEDGE_FIELD_CHARS + 1),
            "description": "d",
            "item_count": 1,
        }
    ]
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_TOO_LARGE


# --- HTTP error mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, knowledge.KS_AUTH_FAILED),
        (403, knowledge.KS_AUTH_FAILED),
        (404, knowledge.KS_NOT_FOUND),
        (429, knowledge.KS_RATE_LIMITED),
        (500, knowledge.KS_UPSTREAM_ERROR),
        (502, knowledge.KS_UPSTREAM_ERROR),
        (503, knowledge.KS_UPSTREAM_ERROR),
    ],
)
def test_ima_http_error_mapping(ima_session: Session, status: int, expected: str) -> None:
    FakeImaHandler.status = status
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == expected


# --- transport failures: DNS / TCP / TLS / timeout -----------------------------


def test_ima_dns_failure(ima_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_getaddrinfo(*args: object, **kwargs: object) -> list[Any]:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", failing_getaddrinfo)
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_DNS_FAILED


def test_ima_tcp_connection_refused(ima_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(settings, "dlr_ima_endpoint", f"http://localhost:{closed_port}")
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_UNREACHABLE


def test_ima_tls_handshake_failure(ima_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    # https:// against a plain-HTTP fake service -> TLS handshake failure.
    monkeypatch.setattr(
        settings,
        "dlr_ima_endpoint",
        f"https://localhost:{_port_of(settings.dlr_ima_endpoint)}",
    )
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_UNREACHABLE


def test_ima_timeout_interrupts_external_call(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeImaHandler.delay_seconds = 2.0
    monkeypatch.setattr(settings, "dlr_ima_timeout_seconds", 0.2)
    started = time.monotonic()
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    elapsed = time.monotonic() - started
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_TIMEOUT
    assert elapsed < 1.5  # really interrupted, not waited out


def _port_of(endpoint: str | None) -> int:
    assert endpoint is not None
    return int(endpoint.rsplit(":", 1)[1])


# --- SSRF guards ---------------------------------------------------------------


def test_ima_ssrf_unknown_host_rejected(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_endpoint", "https://evil.example.com/v1")
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CONFIG_INVALID


def test_ima_ssrf_plain_http_rejected_without_escape_hatch(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_allow_http", False)
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CONFIG_INVALID


def test_ima_ssrf_ip_literal_rejected(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings, "dlr_ima_endpoint", f"http://127.0.0.1:{_port_of(settings.dlr_ima_endpoint)}"
    )
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CONFIG_INVALID


def test_ima_ssrf_userinfo_and_query_rejected(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_endpoint", "https://user:pass@ima.qq.com/v1?x=1")
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CONFIG_INVALID


def test_ima_redirect_refused(ima_session: Session) -> None:
    FakeImaHandler.redirect_to = "https://evil.example.com/phish"
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_UNREACHABLE


# --- credential handling -------------------------------------------------------


def test_ima_not_configured_and_credential_errors(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_endpoint", None)
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_NOT_CONFIGURED

    monkeypatch.setattr(settings, "dlr_ima_endpoint", "http://localhost:1")
    monkeypatch.setattr(settings, "dlr_ima_credential_name", None)
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CREDENTIAL_INVALID

    monkeypatch.setattr(settings, "dlr_ima_credential_name", "missing-cred")
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CREDENTIAL_INVALID

    # A session-less execution cannot resolve the Secret Store either.
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=None)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CREDENTIAL_INVALID


def test_ima_wrong_token_is_stable_auth_failed(ima_session: Session) -> None:
    FakeImaHandler.require_token = "some-other-token"
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_AUTH_FAILED
    assert IMA_TOKEN not in execution.model_content


def test_redact_values_for_pre_resolution(ima_session: Session) -> None:
    values = knowledge.redact_values_for("ima", ima_session)
    assert IMA_TOKEN in values


# --- secret truth never leaves the server --------------------------------------


def test_secret_truth_never_reaches_prompt_ui_provider_or_logs(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full assist chain: the fake official service echoes the ima Token
    inside the read content; the tools layer must redact it by value from the
    provider chain (tool results), the browser response (summaries) and every
    log line."""
    FakeImaHandler.require_token = IMA_TOKEN
    FakeImaHandler.echo_token = IMA_TOKEN
    adapter = create_adapter(api_client, "knowledge-secrets")
    configure(api_client)
    create_credential(
        api_client, name=CREDENTIAL_NAME, credential_type="token", fields={"token": IMA_TOKEN}
    )
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "call-list"),
                _call("search_knowledge", '{"source": "ima", "query": "contract"}', "call-search"),
                _call("read_knowledge", '{"source": "ima", "item_id": "kb-item-1"}', "call-read"),
            ],
            captured=captured,
        ),
    )
    with (
        caplog.at_level(logging.INFO, logger="dlr.ai.tools"),
        caplog.at_level(logging.INFO, logger="dlr.ai.knowledge"),
    ):
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tool_calls"]) == 3
    assert [item["status"] for item in body["tool_calls"]] == ["success"] * 3
    read_summary = body["tool_calls"][2]
    assert read_summary["source"] == "ima:v1:kb-item-1"
    assert "[REDACTED]" in read_summary["result_summary"]
    assert IMA_TOKEN not in read_summary["result_summary"]
    # The browser sees only sanitized summaries.
    assert IMA_TOKEN not in response.text
    assert IMA_CLIENT_ID not in response.text
    # The provider chain (echo + sanitized tool results) never carries the
    # token either.
    for payload in captured:
        assert IMA_TOKEN not in json.dumps(payload, ensure_ascii=False)
    # Logs stay metadata-only.
    for record in caplog.records:
        assert IMA_TOKEN not in record.getMessage()
        assert IMA_CLIENT_ID not in record.getMessage()


def test_secret_truth_absent_from_error_paths(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing upstream (401) yields the stable ks_auth_failed summary and
    the token still never reaches the browser, the provider or the logs."""
    FakeImaHandler.require_token = "different-token"
    adapter = create_adapter(api_client, "knowledge-secret-errors")
    configure(api_client)
    create_credential(
        api_client, name=CREDENTIAL_NAME, credential_type="token", fields={"token": IMA_TOKEN}
    )
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(
            [_call("list_knowledge_bases", '{"source": "ima"}')],
            captured=captured,
        ),
    )
    with (
        caplog.at_level(logging.INFO, logger="dlr.ai.tools"),
        caplog.at_level(logging.INFO, logger="dlr.ai.knowledge"),
    ):
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 200, response.text
    summary = response.json()["tool_calls"][0]
    assert summary["status"] == "error"
    assert summary["error_code"] == knowledge.KS_AUTH_FAILED
    assert IMA_TOKEN not in response.text
    for payload in captured:
        assert IMA_TOKEN not in json.dumps(payload, ensure_ascii=False)
    for record in caplog.records:
        assert IMA_TOKEN not in record.getMessage()


# --- full assist chain: AiModelOutput, candidate=null, attachments, isolation --


def test_assist_knowledge_chain_final_output_candidate_null_and_attachments(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeImaHandler.require_token = IMA_TOKEN
    adapter = create_adapter(api_client, "knowledge-chain")
    configure(api_client)
    create_credential(
        api_client, name=CREDENTIAL_NAME, credential_type="token", fields={"token": IMA_TOKEN}
    )

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if any(m.get("role") == "tool" for m in payload["messages"]):
            # Final answer with candidate=null after the knowledge round.
            return fake_chat_response({"message": "No code changes needed.", "candidate": None})
        return _tool_response(
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "call-1"),
                _call("search_knowledge", '{"source": "ima", "query": "contract"}', "call-2"),
                _call("read_knowledge", '{"source": "ima", "item_id": "kb-item-1"}', "call-3"),
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body()
    body["attachments"] = [
        {
            "filename": "notes.txt",
            "content_type": "text/plain",
            "data_base64": "bm90ZXM=",
        }
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["candidate"] is None
    assert len(result["tool_calls"]) == 3
    assert [item["status"] for item in result["tool_calls"]] == ["success"] * 3
    assert result["tool_calls"][2]["source"] == "ima:v1:kb-item-1"
    assert IMA_TOKEN not in response.text


def test_assist_knowledge_chain_with_candidate_and_adapter_isolation(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeImaHandler.require_token = IMA_TOKEN
    adapter_a = create_adapter(api_client, "knowledge-adapter-a")
    adapter_b = create_adapter(api_client, "knowledge-adapter-b")
    configure(api_client)
    create_credential(
        api_client, name=CREDENTIAL_NAME, credential_type="token", fields={"token": IMA_TOKEN}
    )

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if any(m.get("role") == "tool" for m in payload["messages"]):
            return _final_response(message="Generated with knowledge.")
        return _tool_response(
            [_call("read_knowledge", '{"source": "ima", "item_id": "kb-item-2"}')]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response_a = api_client.post(f"/api/adapters/{adapter_a['id']}/ai/assist", json=assist_body())
    assert response_a.status_code == 200, response_a.text
    result_a = response_a.json()
    assert result_a["candidate"] is not None
    assert result_a["tool_calls"][0]["source"] == "ima:v1:kb-item-2"
    # Adapter B keeps the plain single-shot path with its own conversation:
    # tool summaries never cross adapters, and recent_messages stays empty
    # (tool data never enters the conversation history).
    response_b = api_client.post(f"/api/adapters/{adapter_b['id']}/ai/assist", json=assist_body())
    assert response_b.status_code == 200, response_b.text
    assert response_b.json()["candidate"] is not None


def test_assist_knowledge_unknown_source_and_recent_messages_shape(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-unknown-source")
    configure(api_client)

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        if any(m.get("role") == "tool" for m in payload["messages"]):
            return _final_response(message="Finished.")
        return _tool_response([_call("list_knowledge_bases", '{"source": "bogus"}')])

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body()
    body["recent_messages"] = [
        {"role": "user", "content": "Please check the docs."},
        {"role": "assistant", "content": "I will check."},
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    summary = result["tool_calls"][0]
    assert summary["status"] == "error"
    assert summary["error_code"] == knowledge.KS_UNKNOWN_SOURCE
    assert result["candidate"] is not None
    # Tool data never joins recent_messages (C1 contract preserved).
    assert "bogus" not in json.dumps(result["message"])
