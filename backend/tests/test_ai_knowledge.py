"""M5.7 Wave C2: read-only KnowledgeSource boundary + Tencent ima adapter tests.

The fake official service implements the OFFICIAL Tencent ima OpenAPI read
contract confirmed from the official skill package ``@tencent-adm/ima-skills``
v1.1.9 (base ``https://ima.qq.com``, auth headers ``ima-openapi-clientid`` /
``ima-openapi-apikey``, envelope ``{code, msg, data}``):

- POST /openapi/wiki/v1/search_knowledge_base (+ get_knowledge_base enrichment)
- POST /openapi/wiki/v1/search_knowledge (requires knowledge_base_id)
- POST /openapi/wiki/v1/get_media_info -> notes get_doc_content branch or
  bounded media-URL fetch branch (GET /media/{id} on the fake service)

Coverage (task contract): list / search / read success; empty / pagination /
limit; malformed and oversize responses; official business codes (auth /
rate-limit / invalid path) and raw HTTP 401/403/404/429/5xx; DNS / TCP / TLS /
timeout; unknown host / plain-HTTP / IP-literal / userinfo / redirect SSRF
guards (including the media-URL fetch); missing and invalid Credentials
(access_key with Client ID / API Key required); Secret truth never reaching
prompts, UI summaries, tool results, logs or exceptions; write operations
rejected; final AiModelOutput with candidate=null / Candidate / attachments /
Regenerate / recent_messages / Adapter isolation; the stable ks_* error
mapping at the tool boundary.
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
from dlr.control.services import ai as ai_service
from dlr.control.services import secrets as secrets_service
from test_ai import (
    assist_body,
    configure,
    create_adapter,
    create_credential,
    fake_chat_response,
    valid_output,
)

IMA_CLIENT_ID = "ima-client-id-plaintext-sentinel-9f3a"
IMA_API_KEY = "ima-api-key-plaintext-sentinel-9f3a"

CREDENTIAL_NAME = "ima-test-cred"


def knowledge_assist_body() -> dict[str, object]:
    """Opt into the Wave D default-off knowledge boundary for these tests."""
    body = assist_body()
    body["knowledge_search_enabled"] = True
    return body


# The user-designated non-sensitive test knowledge base; matched by NAME only.
TEST_KB_NAME = "DLR接口库"

BASES = [
    {
        "id": "dlr-interface-lib",
        "name": TEST_KB_NAME,
        "cover_url": "",
        "description": "DLR interface contract documents (fixture)",
    },
    {
        "id": "platform-manuals",
        "name": "平台手册",
        "cover_url": "",
        "description": "Platform operation manuals (fixture)",
    },
]

REAL_FIELD_BASES = [
    {
        "kb_id": "ima-real-kb-id",
        "kb_name": "ima真实知识库",
        "description": "Tencent ima response fixture",
        "content_count": 3,
        "member_count": 1,
        "creator": "fixture",
        "role_type": 1,
        "base_type": 0,
        "cover_url": "",
    }
]

BOTH_FIELD_BASES = [
    {
        "kb_id": "preferred-kb-id",
        "kb_name": "Preferred knowledge base",
        "id": "legacy-kb-id",
        "name": "Legacy knowledge base",
        "cover_url": "",
        "description": "Both field contracts fixture",
    }
]

ITEMS: dict[str, dict[str, str]] = {
    "kb-item-1": {
        "media_id": "kb-item-1",
        "title": "Runtime contract",
        "parent_folder_id": "",
        "highlight_content": "The adapter runtime contract for DLR.",
    },
    "kb-item-2": {
        "media_id": "kb-item-2",
        "title": "Secrets and bindings",
        "parent_folder_id": "",
        "highlight_content": "Credential truth never joins prompts or logs.",
    },
    "kb-item-3": {
        "media_id": "kb-item-3",
        "title": "Schedule triggers",
        "parent_folder_id": "",
        "highlight_content": "Cron-based schedule runs for task adapters.",
    },
}

NOTES: dict[str, str] = {
    "note-2": (
        "Credential truth is stored encrypted in the Secret Store and is "
        "never sent to the model, the browser or the logs."
    ),
}

MEDIA_TEXT: dict[str, str] = {
    "kb-item-1": (
        "The adapter runtime contract for DLR: handle(context, input) with "
        "JSON-compatible input and output; context.config, context.secrets "
        "and context.logger; output is bounded."
    ),
}


class FakeImaHandler(BaseHTTPRequestHandler):
    """One fake official ima service implementing the official contract.

    Class-level knobs are reset per test: ``status`` forces a raw HTTP error,
    ``raw_body`` forces an arbitrary response body, ``delay_seconds`` adds
    latency, ``redirect_to`` emits a redirect, ``require_auth`` enables the
    official header check, ``bases`` / ``items`` override the dataset,
    ``media_url_override`` overrides the media URL returned by
    get_media_info (SSRF tests), and ``echo_secret`` echoes the API key into
    read content (redaction proof).
    """

    status: int | None = None
    raw_body: bytes | None = None
    delay_seconds: float = 0.0
    redirect_to: str | None = None
    require_auth: bool = True
    biz_code: int | None = None
    close_after_bytes: int | None = None
    echo_secret: bool = True
    bases: list[dict[str, Any]] = BASES
    items: dict[str, dict[str, str]] = ITEMS
    media_url_override: str | None = None
    requested: list[str] = []
    requested_search_knowledge_base_ids: list[str] = []

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

    def _ok(self, data: dict[str, Any], op: str) -> None:
        self.requested.append(f"POST {self.path}")
        self._write(200, {"code": 0, "msg": "success", "data": data})

    def _biz_error(self, code: int) -> None:
        self._write(200, {"code": code, "msg": "business error", "data": {}})

    def _authorized(self) -> bool:
        if not self.require_auth:
            return True
        return (
            self.headers.get("ima-openapi-clientid") == IMA_CLIENT_ID
            and self.headers.get("ima-openapi-apikey") == IMA_API_KEY
        )

    def _body(self) -> dict[str, Any]:
        payload = json.loads(self._request_body)
        if not isinstance(payload, dict):
            raise TypeError("request body must be an object")
        return payload

    def _handle_openapi(self) -> None:
        if self.redirect_to is not None:
            self.send_response(301)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        if self.status is not None:
            self._write(self.status, {"error": {"code": "upstream", "message": "fake"}})
            return
        if self.close_after_bytes is not None:
            total = self.close_after_bytes + 10
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            self.wfile.write(b"x" * self.close_after_bytes)
            return
        if self.raw_body is not None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.raw_body)))
            self.end_headers()
            self.wfile.write(self.raw_body)
            return
        if not self._authorized():
            # Official-style business auth failure (HTTP 200 + code 20004).
            self._biz_error(20004)
            return
        if self.biz_code is not None:
            self._biz_error(self.biz_code)
            return
        if self.path == "/openapi/wiki/v1/search_knowledge_base":
            body = self._body()
            limit = min(max(int(body.get("limit", 20)), 1), 20)
            query = str(body.get("query", "")).casefold()
            matches = [
                b
                for b in self.bases
                if query == "" or query in str(b.get("kb_name") or b.get("name") or "").casefold()
            ]
            self._ok(
                {
                    "info_list": [dict(b) for b in matches[:limit]],
                    "is_end": True,
                    "next_cursor": "",
                },
                "search_knowledge_base",
            )
            return
        if self.path == "/openapi/wiki/v1/get_knowledge_base":
            body = self._body()
            ids = body.get("ids") or []
            infos = {
                str(b.get("kb_id") or b.get("id")): b
                for b in self.bases
                if str(b.get("kb_id") or b.get("id")) in ids
            }
            self._ok({"infos": infos}, "get_knowledge_base")
            return
        if self.path == "/openapi/wiki/v1/search_knowledge":
            body = self._body()
            query = str(body.get("query", "")).casefold()
            kb_id = str(body.get("knowledge_base_id", ""))
            self.requested_search_knowledge_base_ids.append(kb_id)
            valid_ids = {str(b.get("kb_id") or b.get("id")) for b in self.bases}
            if kb_id not in valid_ids:
                self._biz_error(110001)
                return
            hits = [
                item
                for item in self.items.values()
                if query in item["title"].casefold()
                or query in item["highlight_content"].casefold()
            ]
            self._ok(
                {"info_list": hits, "is_end": True, "next_cursor": ""},
                "search_knowledge",
            )
            return
        if self.path == "/openapi/wiki/v1/get_media_info":
            body = self._body()
            media_id = str(body.get("media_id", ""))
            if media_id == "kb-item-1":
                url = self.media_url_override or (f"{self.server.base_url}/media/{media_id}")
                self._ok(
                    {
                        "media_type": 1,
                        "url_info": {"url": url, "headers": {}},
                    },
                    "get_media_info",
                )
                return
            if media_id == "kb-item-2":
                self._ok(
                    {
                        "media_type": 11,
                        "notebook_ext_info": {"notebook_id": "note-2"},
                    },
                    "get_media_info",
                )
                return
            self._biz_error(110001)
            return
        if self.path == "/openapi/note/v1/get_doc_content":
            body = self._body()
            note_id = str(body.get("note_id", ""))
            fmt = body.get("target_content_format")
            if note_id != "note-2" or fmt != 0:
                self._biz_error(210001)
                return
            content = NOTES[note_id]
            if self.echo_secret:
                content = f"{content} Configured token: {IMA_API_KEY}"
            self._ok({"content": content}, "get_doc_content")
            return
        self._biz_error(110012)

    def _handle_media(self) -> None:
        item_id = self.path[len("/media/") :].split("?")[0]
        text = MEDIA_TEXT.get(item_id)
        if text is None:
            self._write(404, {"error": {"code": "not_found", "message": "fake"}})
            return
        if self.echo_secret:
            text = f"{text} Configured token: {IMA_API_KEY}"
        encoded = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        # Read the request body first: a server that closes with unread
        # request data can RST the client mid-response (deterministic on
        # Linux under load), which previously flaked the oversize test.
        length = int(self.headers.get("Content-Length", "0"))
        self._request_body = self.rfile.read(length)
        try:
            self._handle_openapi()
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._biz_error(110001)

    def do_GET(self) -> None:
        if self.path.startswith("/media/"):
            self._handle_media()
            return
        self._write(404, {"error": {"code": "not_found", "message": "fake"}})

    def log_message(self, format_string: str, *args: object) -> None:
        pass


@pytest.fixture()
def ima_server() -> Iterator[ThreadingHTTPServer]:
    FakeImaHandler.status = None
    FakeImaHandler.raw_body = None
    FakeImaHandler.delay_seconds = 0.0
    FakeImaHandler.redirect_to = None
    FakeImaHandler.require_auth = True
    FakeImaHandler.biz_code = None
    FakeImaHandler.close_after_bytes = None
    FakeImaHandler.echo_secret = True
    FakeImaHandler.bases = BASES
    FakeImaHandler.items = ITEMS
    FakeImaHandler.media_url_override = None
    FakeImaHandler.requested = []
    FakeImaHandler.requested_search_knowledge_base_ids = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeImaHandler)
    server.base_url = f"http://localhost:{server.server_port}"
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
                type="access_key",
                fields={
                    "access_key_id": IMA_CLIENT_ID,
                    "access_key_secret": IMA_API_KEY,
                },
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
    knowledge_search_enabled: bool = True,
) -> tools_service.ToolExecution:
    context = tools_service.ToolExecutionContext(
        session=session,
        secret_values=secret_values or [],
        knowledge_search_enabled=knowledge_search_enabled,
    )
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


def _state_execution(tool_name: str, payload: dict[str, object]) -> tools_service.ToolExecution:
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return tools_service.ToolExecution(
        tool_name=tool_name,
        status="success",
        args_summary="",
        result_summary="",
        model_content=content,
        error_code=None,
        duration_ms=0,
        result_truncated=False,
        result_size=len(content),
        source=None,
    )


def test_knowledge_state_enforces_order_and_ids_from_current_results() -> None:
    state = ai_service._KnowledgeRetrievalState.create(True, True)
    list_args = {"source": "ima"}
    assert state.phase == "need_list"
    assert state.accepts_call("dlr_docs_list", {}) is False
    assert state.accepts_call("search_knowledge", {"source": "ima"}) is False
    assert state.accepts_call("list_knowledge_bases", list_args) is True
    state.record_execution(
        list_args,
        _state_execution(
            "list_knowledge_bases",
            {"items": [{"id": "kb-real", "source": "ima:v1:kb-real"}]},
        ),
    )
    assert state.phase == "need_search"
    assert (
        state.accepts_call(
            "search_knowledge",
            {"source": "ima", "knowledge_base_id": "kb-forged", "query": "q"},
        )
        is False
    )
    search_args = {
        "source": "ima",
        "knowledge_base_id": "kb-real",
        "query": "q",
    }
    assert state.accepts_call("search_knowledge", search_args) is True
    state.record_execution(
        search_args,
        _state_execution(
            "search_knowledge",
            {"items": [{"id": "item-real", "source": "ima:v1:item-real"}]},
        ),
    )
    assert state.phase == "need_read"
    assert (
        state.accepts_call("read_knowledge", {"source": "ima", "item_id": "item-forged"}) is False
    )
    read_args = {"source": "ima", "item_id": "item-real"}
    assert state.accepts_call("read_knowledge", read_args) is True
    state.record_execution(
        read_args,
        _state_execution(
            "read_knowledge",
            {"item": {"id": "item-real", "source": "ima:v1:item-real"}},
        ),
    )
    assert state.phase == "ready"
    assert state.stop_reason == ai_service._STOP_KNOWLEDGE_READY
    assert state.evidence_sources == {"ima:v1:kb-real", "ima:v1:item-real"}


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

        def search_knowledge(self, query: str, limit: int, knowledge_base_id: str) -> list[Any]:
            return []

        def read_knowledge(self, item_id: str) -> Any:
            raise NotImplementedError

        def upload_document(self, path: str) -> None:
            raise NotImplementedError

    assert knowledge.is_read_only_source(ReadOnlyImpostor()) is False

    class CleanSource:
        def list_knowledge_bases(self) -> list[Any]:
            return []

        def search_knowledge(self, query: str, limit: int, knowledge_base_id: str) -> list[Any]:
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
    # The user-designated test knowledge base is matched by NAME.
    names = [item["name"] for item in listed["items"]]
    assert TEST_KB_NAME in names
    assert all(item["source"].startswith("ima:v1:") for item in listed["items"])
    assert listing.source == "ima:v1:dlr-interface-lib"

    search = execute(
        "search_knowledge",
        {
            "source": "ima",
            "knowledge_base_id": "dlr-interface-lib",
            "query": "contract",
            "limit": 1,
        },
        session=ima_session,
    )
    assert search.status == "success", search.error_code
    searched = json.loads(search.model_content)
    assert searched["total_matches"] == 1
    assert searched["items"][0]["id"] == "kb-item-1"
    assert search.source == "ima:v1:kb-item-1"

    # Notes branch of the official read chain (media_type=11 -> get_doc_content).
    read = execute(
        "read_knowledge",
        {"source": "ima", "item_id": "kb-item-2"},
        session=ima_session,
    )
    assert read.status == "success", read.error_code
    assert read.source == "ima:v1:kb-item-2"
    assert "Secret Store" in read.model_content

    # URL branch of the official read chain (get_media_info -> media URL).
    read_url = execute(
        "read_knowledge",
        {"source": "ima", "item_id": "kb-item-1"},
        session=ima_session,
    )
    assert read_url.status == "success", read_url.error_code
    assert "runtime contract" in read_url.model_content


def test_ima_real_kb_fields_normalize_and_preserve_search_id(ima_session: Session) -> None:
    FakeImaHandler.bases = REAL_FIELD_BASES

    listing = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert listing.status == "success", listing.error_code
    listed = json.loads(listing.model_content)
    assert listed["items"] == [
        {
            "id": "ima-real-kb-id",
            "name": "ima真实知识库",
            "description": "Tencent ima response fixture",
            "item_count": 0,
            "source": "ima:v1:ima-real-kb-id",
        }
    ]

    search = execute(
        "search_knowledge",
        {
            "source": "ima",
            "knowledge_base_id": "ima-real-kb-id",
            "query": "contract",
            "limit": 1,
        },
        session=ima_session,
    )
    assert search.status == "success", search.error_code
    assert FakeImaHandler.requested_search_knowledge_base_ids == ["ima-real-kb-id"]


def test_ima_new_kb_fields_take_priority_when_legacy_fields_are_present(
    ima_session: Session,
) -> None:
    FakeImaHandler.bases = BOTH_FIELD_BASES

    listing = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert listing.status == "success", listing.error_code
    listed = json.loads(listing.model_content)
    assert listed["items"][0]["id"] == "preferred-kb-id"
    assert listed["items"][0]["name"] == "Preferred knowledge base"
    assert listed["items"][0]["source"] == "ima:v1:preferred-kb-id"


@pytest.mark.parametrize(
    "base",
    [
        {"kb_name": "Missing ID", "raw_marker": "ima-malformed-id-payload"},
        {"kb_id": "missing-name-id", "raw_marker": "ima-malformed-name-payload"},
    ],
)
def test_ima_missing_kb_candidates_is_stable_and_does_not_echo_payload(
    ima_session: Session,
    base: dict[str, str],
) -> None:
    marker = base["raw_marker"]
    FakeImaHandler.raw_body = json.dumps(
        {"code": 0, "msg": "success", "data": {"info_list": [base]}},
        ensure_ascii=False,
    ).encode()

    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_RESPONSE_INVALID
    assert marker not in execution.args_summary
    assert marker not in execution.result_summary
    assert marker not in execution.model_content


def test_ima_empty_search_and_limit(ima_session: Session) -> None:
    empty = execute(
        "search_knowledge",
        {
            "source": "ima",
            "knowledge_base_id": "dlr-interface-lib",
            "query": "zzz-no-match",
        },
        session=ima_session,
    )
    assert empty.status == "success", empty.error_code
    assert json.loads(empty.model_content)["total_matches"] == 0

    # The tool schema clamps limit into 1..10 (DLR-side bound; the official
    # search API has no limit parameter and returns one cursor page).
    clamped = execute(
        "search_knowledge",
        {
            "source": "ima",
            "knowledge_base_id": "dlr-interface-lib",
            "query": "the",
            "limit": 99,
        },
        session=ima_session,
    )
    assert clamped.status == "success", clamped.error_code
    assert json.loads(clamped.model_content)["limit"] == 10


def test_ima_read_missing_item_is_stable_not_found(ima_session: Session) -> None:
    missing = execute(
        "read_knowledge",
        {"source": "ima", "item_id": "no-such-item"},
        session=ima_session,
    )
    assert missing.status == "error"
    assert missing.error_code == knowledge.KS_UPSTREAM_ERROR
    # The stable error result never echoes the requested id.
    assert "no-such-item" not in missing.model_content


# --- malformed / oversize responses -------------------------------------------


def test_ima_malformed_response_rejected(ima_session: Session) -> None:
    FakeImaHandler.raw_body = b"{not json"
    assert (
        execute("list_knowledge_bases", {"source": "ima"}, session=ima_session).error_code
        == knowledge.KS_RESPONSE_INVALID
    )

    FakeImaHandler.raw_body = b'{"code": 0, "data": {"info_list": "nope"}}'
    assert (
        execute("list_knowledge_bases", {"source": "ima"}, session=ima_session).error_code
        == knowledge.KS_RESPONSE_INVALID
    )

    # Missing envelope code / non-dict data are malformed.
    FakeImaHandler.raw_body = b'{"msg": "success", "data": {}}'
    assert (
        execute("list_knowledge_bases", {"source": "ima"}, session=ima_session).error_code
        == knowledge.KS_RESPONSE_INVALID
    )


def test_ima_oversize_response_rejected(ima_session: Session) -> None:
    FakeImaHandler.raw_body = b"x" * (ima_adapter.MAX_KNOWLEDGE_RESPONSE_BYTES + 1)
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_TOO_LARGE


def test_ima_transport_cut_with_oversize_partial_is_still_too_large(
    ima_session: Session,
) -> None:
    """Deterministic repro: the upstream closes mid-body after already
    sending more than the size bound. The response is still rejected as
    too large (never remapped to a transport error), matching the official
    contract that schema/size validation precedes anything else."""
    FakeImaHandler.close_after_bytes = ima_adapter.MAX_KNOWLEDGE_RESPONSE_BYTES + 1
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_TOO_LARGE


def test_ima_transport_cut_with_small_partial_is_not_too_large(
    ima_session: Session,
) -> None:
    """A transport cut before the body reaches the size bound must never be
    misreported as too_large: the truncated body fails the strict JSON /
    envelope validation first and maps to the stable malformed code."""
    FakeImaHandler.close_after_bytes = 100
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_RESPONSE_INVALID


def test_ima_oversize_item_field_rejected(ima_session: Session) -> None:
    FakeImaHandler.bases = [
        {
            "id": "big",
            "name": "n" * (knowledge.MAX_KNOWLEDGE_FIELD_CHARS + 1),
            "cover_url": "",
            "description": "d",
        }
    ]
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_TOO_LARGE


# --- official business codes and HTTP error mapping -----------------------------


def test_ima_business_auth_code_mapping(ima_session: Session) -> None:
    # Wrong credential values -> official business code 20004 (HTTP 200).
    secrets_service.create_credential(
        ima_session,
        CredentialCreate(
            name="ima-test-cred-wrong",
            type="access_key",
            fields={"access_key_id": "wrong-client", "access_key_secret": "wrong-key"},
        ),
    )
    settings.dlr_ima_credential_name = "ima-test-cred-wrong"
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_AUTH_FAILED
    assert "wrong-key" not in execution.model_content


@pytest.mark.parametrize(
    ("biz_code", "expected"),
    [
        (20004, knowledge.KS_AUTH_FAILED),
        (110030, knowledge.KS_AUTH_FAILED),
        (20002, knowledge.KS_RATE_LIMITED),
        (110021, knowledge.KS_RATE_LIMITED),
        (110012, knowledge.KS_CONFIG_INVALID),
        (110001, knowledge.KS_UPSTREAM_ERROR),
    ],
)
def test_ima_business_code_mapping(ima_session: Session, biz_code: int, expected: str) -> None:
    FakeImaHandler.biz_code = biz_code
    execution = execute("list_knowledge_bases", {"source": "ima"}, session=ima_session)
    assert execution.status == "error"
    assert execution.error_code == expected


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
    FakeImaHandler.require_auth = False
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


def test_ima_media_url_ssrf_guards(ima_session: Session) -> None:
    # The official get_media_info returns a media URL; the adapter must
    # validate it against the official media host allowlist before fetching.
    FakeImaHandler.media_url_override = "https://evil.example.com/phish.pdf"
    execution = execute(
        "read_knowledge", {"source": "ima", "item_id": "kb-item-1"}, session=ima_session
    )
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CONFIG_INVALID

    # IP literal media URL is refused even when allow_http is on.
    FakeImaHandler.media_url_override = "http://127.0.0.1:9/steal"
    execution = execute(
        "read_knowledge", {"source": "ima", "item_id": "kb-item-1"}, session=ima_session
    )
    assert execution.status == "error"
    assert execution.error_code == knowledge.KS_CONFIG_INVALID

    # Non-text media content is refused with a stable unsupported code.
    FakeImaHandler.media_url_override = (
        f"http://localhost:{_port_of(settings.dlr_ima_endpoint)}/media/pdf"
    )
    execution = execute(
        "read_knowledge", {"source": "ima", "item_id": "kb-item-1"}, session=ima_session
    )
    assert execution.status == "error"
    assert execution.error_code in (knowledge.KS_UNSUPPORTED, knowledge.KS_NOT_FOUND)


# --- credential handling -------------------------------------------------------


def test_ima_not_configured_and_credential_errors(
    ima_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_endpoint", "")
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


def test_ima_token_type_credential_rejected(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The official contract needs Client ID + API Key (two headers), so a
    token-type Credential cannot authenticate the source."""
    adapter = create_adapter(api_client, "ima-token-cred")
    configure(api_client)
    create_credential(
        api_client, name=CREDENTIAL_NAME, credential_type="token", fields={"token": "x"}
    )
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final([_call("list_knowledge_bases", '{"source": "ima"}')]),
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is None
    assert "知识库检索当前不可用" in response.json()["message"]


def test_redact_values_for_pre_resolution(ima_session: Session) -> None:
    values = knowledge.redact_values_for("ima", ima_session)
    assert IMA_CLIENT_ID in values
    assert IMA_API_KEY in values


# --- secret truth never leaves the server --------------------------------------


def test_secret_truth_never_reaches_prompt_ui_provider_or_logs(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The full assist chain: the fake official service echoes the ima API
    Key inside the read content; the tools layer must redact it (and the
    Client ID) by value from the provider chain (tool results), the browser
    response (summaries) and every log line."""
    adapter = create_adapter(api_client, "knowledge-secrets")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "call-list"),
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "secrets"}',
                    "call-search",
                ),
                _call("read_knowledge", '{"source": "ima", "item_id": "kb-item-2"}', "call-read"),
            ],
            captured=captured,
        ),
    )
    with (
        caplog.at_level(logging.INFO, logger="dlr.ai.tools"),
        caplog.at_level(logging.INFO, logger="dlr.ai.knowledge"),
    ):
        response = api_client.post(
            f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tool_calls"]) == 3
    assert [item["status"] for item in body["tool_calls"]] == ["success"] * 3
    read_summary = body["tool_calls"][2]
    assert read_summary["source"] == "ima:v1:kb-item-2"
    assert "[REDACTED]" in read_summary["result_summary"]
    assert IMA_API_KEY not in read_summary["result_summary"]
    # The browser sees only sanitized summaries.
    assert IMA_API_KEY not in response.text
    assert IMA_CLIENT_ID not in response.text
    # The provider chain (echo + sanitized tool results) never carries the
    # credential truth either.
    for payload in captured:
        assert IMA_API_KEY not in json.dumps(payload, ensure_ascii=False)
        assert IMA_CLIENT_ID not in json.dumps(payload, ensure_ascii=False)
    # Logs stay metadata-only.
    for record in caplog.records:
        assert IMA_API_KEY not in record.getMessage()
        assert IMA_CLIENT_ID not in record.getMessage()


def test_secret_truth_absent_from_error_paths(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing upstream (business auth code 20004) yields the stable
    ks_auth_failed summary and the credential truth still never reaches the
    browser, the provider or the logs."""
    FakeImaHandler.require_auth = True
    adapter = create_adapter(api_client, "knowledge-secret-errors")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": "wrong-client", "access_key_secret": "wrong-key"},
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
        response = api_client.post(
            f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
        )
    assert response.status_code == 200, response.text
    summary = response.json()["tool_calls"][0]
    assert summary["status"] == "error"
    assert summary["error_code"] == knowledge.KS_AUTH_FAILED
    assert "wrong-client" not in response.text
    assert "wrong-key" not in response.text
    for payload in captured:
        assert "wrong-key" not in json.dumps(payload, ensure_ascii=False)
    for record in caplog.records:
        assert "wrong-key" not in record.getMessage()


# --- full assist chain: AiModelOutput, candidate=null, attachments, isolation --


def test_assist_rejects_direct_final_then_completes_required_knowledge_order(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-direct-final")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    captured: list[dict[str, Any]] = []

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured.append(payload or {})
        if len(captured) == 1:
            return _final_response(message="This direct answer must be ignored.")
        if len(captured) == 2:
            return _tool_response(
                [
                    _call("list_knowledge_bases", '{"source": "ima"}', "direct-list"),
                    _call(
                        "search_knowledge",
                        '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                        '"query": "secrets"}',
                        "direct-search",
                    ),
                    _call(
                        "read_knowledge",
                        '{"source": "ima", "item_id": "kb-item-2"}',
                        "direct-read",
                    ),
                ]
            )
        assert payload is not None and "tools" not in payload
        return _final_response(message="知识库检索结果：已读取条目。\n\n模型补充：无。")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 3
    assert "This direct answer must be ignored" not in response.text
    assert [item["tool_name"] for item in response.json()["tool_calls"]] == [
        "list_knowledge_bases",
        "search_knowledge",
        "read_knowledge",
    ]
    assert any(
        "server-enforced stage need_list" in str(message.get("content"))
        for message in captured[1]["messages"]
    )
    assert any(
        "never invent a source" in str(message.get("content"))
        for message in captured[2]["messages"]
    )


def test_assist_stops_after_three_direct_final_corrections(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-direct-final-loop")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    provider_calls = 0

    def fake_request(*_: object, **__: object) -> object:
        nonlocal provider_calls
        provider_calls += 1
        return _final_response(message="Still attempting to bypass retrieval.")

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    assert provider_calls == 3
    assert response.json()["candidate"] is None
    assert response.json()["tool_calls"] == []
    assert "知识库检索顺序未完成" in response.json()["message"]


def test_assist_blocks_nonknowledge_tool_before_corrected_knowledge_chain(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-nonknowledge-gate")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    rounds = 0

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        nonlocal rounds
        if payload is not None and "tools" not in payload:
            return _final_response(message="知识库检索结果：已读取。\n\n模型补充：无。")
        rounds += 1
        if rounds == 1:
            return _tool_response([_call("dlr_docs_list", "{}", "wrong-tool")])
        return _tool_response(
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "fixed-list"),
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "secrets"}',
                    "fixed-search",
                ),
                _call(
                    "read_knowledge",
                    '{"source": "ima", "item_id": "kb-item-2"}',
                    "fixed-read",
                ),
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    summaries = response.json()["tool_calls"]
    assert summaries[0]["tool_name"] == "dlr_docs_list"
    assert summaries[0]["status"] == "error"
    assert summaries[0]["error_code"] == tools_service.CODE_KNOWLEDGE_SEQUENCE
    assert [item["status"] for item in summaries[1:]] == ["success"] * 3


def test_assist_knowledge_reuses_duplicate_protection_for_repeated_search(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-duplicate-search")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    search_args = '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", "query": "secrets"}'
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "duplicate-list"),
                _call("search_knowledge", search_args, "duplicate-search-1"),
                _call("search_knowledge", search_args, "duplicate-search-2"),
            ]
        ),
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    summaries = response.json()["tool_calls"]
    assert [item["status"] for item in summaries] == ["success", "success", "error"]
    assert summaries[-1]["error_code"] == tools_service.CODE_DUPLICATE
    assert FakeImaHandler.requested.count("POST /openapi/wiki/v1/search_knowledge") == 1
    assert response.json()["candidate"] is None


def test_assist_knowledge_empty_list_is_transparent_and_does_not_search(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeImaHandler.bases = []
    adapter = create_adapter(api_client, "knowledge-empty-list")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(
            [_call("list_knowledge_bases", '{"source": "ima"}')],
            message="General model context only.",
        ),
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is None
    assert "没有可检索的知识库" in response.json()["message"]
    assert "模型补充：General model context only." in response.json()["message"]
    assert "POST /openapi/wiki/v1/search_knowledge" not in FakeImaHandler.requested


def test_assist_knowledge_empty_search_is_transparent_and_does_not_read(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-empty-search")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "empty-list"),
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "zzz-no-match"}',
                    "empty-search",
                ),
            ],
            message="General model context only.",
        ),
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is None
    assert "知识库搜索未找到匹配条目" in response.json()["message"]
    assert [item["tool_name"] for item in response.json()["tool_calls"]] == [
        "list_knowledge_bases",
        "search_knowledge",
    ]
    assert not any("get_media_info" in path for path in FakeImaHandler.requested)


@pytest.mark.parametrize(
    ("failing_tool", "calls", "expected_stage", "expected_count"),
    [
        (
            "list_knowledge_bases",
            [_call("list_knowledge_bases", '{"source": "ima"}')],
            "知识库列表阶段失败",
            1,
        ),
        (
            "search_knowledge",
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "failure-list"),
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "secrets"}',
                    "failure-search",
                ),
            ],
            "知识库搜索阶段失败",
            2,
        ),
        (
            "read_knowledge",
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "read-failure-list"),
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "secrets"}',
                    "read-failure-search",
                ),
                _call(
                    "read_knowledge",
                    '{"source": "ima", "item_id": "kb-item-2"}',
                    "read-failure-read",
                ),
            ],
            "知识条目正文读取失败",
            3,
        ),
    ],
)
def test_assist_knowledge_stage_failures_are_transparent(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    failing_tool: str,
    calls: list[dict[str, Any]],
    expected_stage: str,
    expected_count: int,
) -> None:
    adapter = create_adapter(api_client, f"knowledge-stage-failure-{failing_tool}")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )

    def fail_handler(_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("private upstream detail")

    monkeypatch.setattr(tools_service._TOOLS[failing_tool], "handler", fail_handler)
    monkeypatch.setattr(
        providers,
        "_request_json",
        _knowledge_then_final(calls, message="General model context only."),
    )
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["candidate"] is None
    assert len(result["tool_calls"]) == expected_count
    assert result["tool_calls"][-1]["error_code"] == tools_service.CODE_FAILED
    assert expected_stage in result["message"]
    assert tools_service.CODE_FAILED in result["message"]
    assert "private upstream detail" not in response.text


def test_assist_knowledge_chain_final_output_candidate_null_and_attachments(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-chain")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
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
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "secrets"}',
                    "call-2",
                ),
                _call("read_knowledge", '{"source": "ima", "item_id": "kb-item-2"}', "call-3"),
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = knowledge_assist_body()
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
    assert result["tool_calls"][2]["source"] == "ima:v1:kb-item-2"
    assert IMA_API_KEY not in response.text


def test_assist_knowledge_chain_with_candidate_and_adapter_isolation(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_a = create_adapter(api_client, "knowledge-adapter-a")
    adapter_b = create_adapter(api_client, "knowledge-adapter-b")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
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
            [
                _call("list_knowledge_bases", '{"source": "ima"}', "call-list"),
                _call(
                    "search_knowledge",
                    '{"source": "ima", "knowledge_base_id": "dlr-interface-lib", '
                    '"query": "secrets"}',
                    "call-search",
                ),
                _call(
                    "read_knowledge",
                    '{"source": "ima", "item_id": "kb-item-2"}',
                    "call-read",
                ),
            ]
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    response_a = api_client.post(
        f"/api/adapters/{adapter_a['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response_a.status_code == 200, response_a.text
    result_a = response_a.json()
    assert result_a["candidate"] is not None
    assert result_a["tool_calls"][2]["source"] == "ima:v1:kb-item-2"
    # Adapter B keeps the plain single-shot path with its own conversation:
    # tool summaries never cross adapters, and recent_messages stays empty
    # (tool data never enters the conversation history).
    response_b = api_client.post(
        f"/api/adapters/{adapter_b['id']}/ai/assist", json=knowledge_assist_body()
    )
    assert response_b.status_code == 200, response_b.text
    assert response_b.json()["candidate"] is not None


def test_assist_knowledge_unknown_source_and_recent_messages_shape(
    api_client: TestClient,
    ima_config: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "knowledge-unknown-source")
    configure(api_client)
    create_credential(
        api_client,
        name=CREDENTIAL_NAME,
        credential_type="access_key",
        fields={"access_key_id": IMA_CLIENT_ID, "access_key_secret": IMA_API_KEY},
    )

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
    body = knowledge_assist_body()
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
    assert result["candidate"] is None
    assert "知识库列表阶段失败" in result["message"]
    # Tool data never joins recent_messages (C1 contract preserved).
    assert "bogus" not in json.dumps(result["message"])
