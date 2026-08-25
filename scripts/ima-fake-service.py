"""Smoke-only local fake official ima-compatible knowledge service.

This server is launched as a temporary one-off container by compose-smoke.sh.
It implements the OFFICIAL Tencent ima OpenAPI read-only contract confirmed
from the official skill package ``@tencent-adm/ima-skills`` v1.1.9 (base
``https://ima.qq.com``, auth headers ``ima-openapi-clientid`` /
``ima-openapi-apikey``, response envelope ``{code, msg, data}``):

- POST /openapi/wiki/v1/search_knowledge_base  (list knowledge bases)
- POST /openapi/wiki/v1/get_knowledge_base     (KB description enrichment)
- POST /openapi/wiki/v1/search_knowledge       (search inside one KB)
- POST /openapi/wiki/v1/get_media_info         (media read chain entry)
- POST /openapi/note/v1/get_doc_content        (notes branch of the read chain)
- GET  /media/{id}                             (URL branch of the read chain)

The smoke then proves list -> search -> read -> final AiModelOutput plus
by-value credential redaction: the read content echoes the request's API Key
on purpose, and the smoke asserts it never reaches the browser, the model
chain or the logs. The knowledge base "DLR接口库" is the user-designated
non-sensitive test target; only its name is asserted, never its id or
content. It deliberately has no production configuration or external network
calls, and it implements NO write interface.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# The user-designated non-sensitive test knowledge base. Tests/smoke match it
# by NAME only; its id is never recorded in reports, PRs or logs.
TEST_KB_NAME = "DLR接口库"

BASES = [
    {
        "id": "dlr-interface-lib",
        "name": TEST_KB_NAME,
        "cover_url": "",
        "description": "DLR interface contract documents (smoke fixture)",
    },
    {
        "id": "platform-manuals",
        "name": "平台手册",
        "cover_url": "",
        "description": "Platform operation manuals (smoke fixture)",
    },
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
    "browser-success-item": {
        "media_id": "browser-success-item",
        "title": "Browser success fixture",
        "parent_folder_id": "",
        "highlight_content": "Safe bounded runtime guidance for browser acceptance.",
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
        "and context.logger; output is bounded and Credential truth never "
        "joins logs."
    ),
    "browser-success-item": (
        "Browser acceptance fixture: bounded runtime guidance is evidence-scoped, "
        "read-only, and requires human review. "
    )
    * 45,
}


class RequestMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def increment(self, op: str) -> None:
        with self._lock:
            self._counts[op] = self._counts.get(op, 0) + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


METRICS = RequestMetrics()


class Handler(BaseHTTPRequestHandler):
    server_version = "DLRFakeIma/1.0"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        expected_client_id = os.environ.get("SMOKE_IMA_CLIENT_ID", "")
        expected_api_key = os.environ.get("SMOKE_IMA_TOKEN", "")
        return (
            bool(expected_client_id)
            and bool(expected_api_key)
            and self.headers.get("ima-openapi-clientid") == expected_client_id
            and self.headers.get("ima-openapi-apikey") == expected_api_key
        )

    def _ok(self, data: dict[str, Any], op: str) -> None:
        METRICS.increment(op)
        self._write_json(200, {"code": 0, "msg": "success", "data": data})

    def _biz_error(self, code: int, msg: str) -> None:
        self._write_json(200, {"code": code, "msg": msg, "data": {}})

    def _body(self) -> dict[str, Any]:
        payload = json.loads(self._request_body)
        if not isinstance(payload, dict):
            raise TypeError("request body must be an object")
        return payload

    def _handle_openapi(self) -> None:
        if not self._authorized():
            # Official-style business auth failure (HTTP 200 + code 20004).
            self._biz_error(20004, "apiKey 鉴权失败")
            return
        if self.path == "/openapi/wiki/v1/search_knowledge_base":
            body = self._body()
            limit = min(max(int(body.get("limit", 20)), 1), 20)
            query = str(body.get("query", "")).casefold()
            matches = [
                base for base in BASES if query in base["name"].casefold() or query == ""
            ]
            self._ok(
                {
                    "info_list": [
                        {"id": b["id"], "name": b["name"], "cover_url": b["cover_url"]}
                        for b in matches[:limit]
                    ],
                    "is_end": True,
                    "next_cursor": "",
                },
                "search_knowledge_base",
            )
            return
        if self.path == "/openapi/wiki/v1/get_knowledge_base":
            body = self._body()
            ids = body.get("ids") or []
            infos = {b["id"]: b for b in BASES if b["id"] in ids}
            self._ok({"infos": infos}, "get_knowledge_base")
            return
        if self.path == "/openapi/wiki/v1/search_knowledge":
            body = self._body()
            query = str(body.get("query", "")).casefold()
            kb_id = str(body.get("knowledge_base_id", ""))
            if kb_id != "dlr-interface-lib":
                self._biz_error(110001, "参数非法")
                return
            if query == "browser-failure":
                self._biz_error(110021, "浏览器验收固定限流错误")
                return
            if query == "browser-empty":
                hits = []
            elif query == "browser-success":
                hits = [ITEMS["browser-success-item"]]
            else:
                hits = [
                    item
                    for item in ITEMS.values()
                    if query in item["title"].casefold()
                    or query in item["highlight_content"].casefold()
                ]
            self._ok(
                {
                    "info_list": hits,
                    "is_end": True,
                    "next_cursor": "",
                },
                "search_knowledge",
            )
            return
        if self.path == "/openapi/wiki/v1/get_media_info":
            body = self._body()
            media_id = str(body.get("media_id", ""))
            if media_id in {"kb-item-1", "browser-success-item"}:
                # The media URL points back at this fake service using the
                # hostname the Control container actually resolves (the
                # container name on the private network); the adapter must
                # allowlist that host before fetching it.
                self._ok(
                    {
                        "media_type": 1,
                        "url_info": {
                            "url": f"{self.server.base_url}/media/{media_id}",
                            "headers": {},
                        },
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
            self._biz_error(110001, "参数非法")
            return
        if self.path == "/openapi/note/v1/get_doc_content":
            body = self._body()
            note_id = str(body.get("note_id", ""))
            fmt = body.get("target_content_format")
            if note_id != "note-2" or fmt != 0:
                self._biz_error(210001, "参数错误")
                return
            token = os.environ.get("SMOKE_IMA_TOKEN", "")
            content = NOTES[note_id]
            if token:
                content = f"{content} Configured token: {token}"
            self._ok({"content": content}, "get_doc_content")
            return
        self._biz_error(110012, "接口无效")

    def _handle_media(self) -> None:
        # The URL branch of the read chain: plain text served from the same
        # fake service; echoes the API key so the smoke proves redaction.
        item_id = self.path[len("/media/") :].split("?")[0]
        text = MEDIA_TEXT.get(item_id)
        if text is None:
            self._write_json(404, {"error": {"code": "not_found", "message": "fake"}})
            return
        token = os.environ.get("SMOKE_IMA_TOKEN", "")
        if token:
            text = f"{text} Configured token: {token}"
        encoded = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        if self.path == "/healthz":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/_smoke/metrics":
            self._write_json(200, METRICS.snapshot())
            return
        # Read the request body first: a server that closes with unread
        # request data can RST the client mid-response on Linux.
        length = int(self.headers.get("Content-Length", "0"))
        self._request_body = self.rfile.read(length)
        try:
            self._handle_openapi()
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            self._biz_error(110001, "参数非法")

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._write_json(200, {"status": "ok"})
            return
        if self.path == "/_smoke/metrics":
            self._write_json(200, METRICS.snapshot())
            return
        if self.path.startswith("/media/"):
            self._handle_media()
            return
        self._write_json(404, {"error": {"code": "not_found", "message": "fake"}})

    def log_message(self, format_string: str, *args: object) -> None:
        # Keep smoke logs useful without ever logging headers or bodies.
        print(f"ima-fake: {format_string % args}", flush=True)


class FakeImaServer(ThreadingHTTPServer):
    base_url = ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    server = FakeImaServer((args.host, args.port), Handler)
    # The base URL the Control container uses to reach this fake service
    # (e.g. http://<container-name>:18081). Falls back to localhost.
    server.base_url = os.environ.get(
        "SMOKE_IMA_BASE_URL", f"http://127.0.0.1:{args.port}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
