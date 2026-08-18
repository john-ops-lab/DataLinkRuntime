"""Smoke-only local fake official ima-compatible knowledge service.

This server is launched as a temporary one-off container by compose-smoke.sh.
It implements DLR's normalized read-only knowledge wire protocol v1 (see
backend/src/dlr/control/ai/ima.py) so the smoke can exercise the real
adapter code end to end: list -> search -> read -> final AiModelOutput, plus
by-value credential redaction (the read content echoes the request's Bearer
token on purpose, and the smoke asserts the token never reaches the browser
or the logs).

It deliberately has no production configuration or external network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BASES = [
    {
        "id": "team-knowledge",
        "name": "Team knowledge base",
        "description": "Team internal documentation for the smoke run",
        "item_count": 3,
    },
    {
        "id": "platform-manuals",
        "name": "Platform manuals",
        "description": "Platform operation manuals for the smoke run",
        "item_count": 2,
    },
]

ITEMS: dict[str, dict[str, str]] = {
    "kb-item-1": {
        "id": "kb-item-1",
        "title": "Runtime contract",
        "content": (
            "The adapter runtime contract for DLR: handle(context, input) with "
            "JSON-compatible input and output; context.config, context.secrets "
            "and context.logger; output is bounded and Credential truth never "
            "joins logs."
        ),
    },
    "kb-item-2": {
        "id": "kb-item-2",
        "title": "Secrets and bindings",
        "content": (
            "Credential truth is stored encrypted in the Secret Store and is "
            "never sent to the model, the browser or the logs."
        ),
    },
    "kb-item-3": {
        "id": "kb-item-3",
        "title": "Schedule triggers",
        "content": (
            "Task adapters run manually or on a schedule; schedule runs keep "
            "an independent cursor and never mutate the schedule configuration."
        ),
    },
}


class RequestMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._list = 0
        self._search = 0
        self._read = 0

    def increment(self, op: str) -> None:
        with self._lock:
            if op == "list":
                self._list += 1
            elif op == "search":
                self._search += 1
            else:
                self._read += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"list": self._list, "search": self._search, "read": self._read}


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
        expected = os.environ.get("SMOKE_IMA_TOKEN", "")
        return bool(expected) and self.headers.get("Authorization") == f"Bearer {expected}"

    def _bases(self) -> dict[str, Any]:
        METRICS.increment("list")
        return {"total": len(BASES), "items": BASES}

    def _search(self, payload: dict[str, Any]) -> dict[str, Any]:
        METRICS.increment("search")
        query = str(payload.get("query", "")).casefold()
        limit = min(max(int(payload.get("limit", 5)), 1), 10)
        hits = [
            {
                "id": item["id"],
                "title": item["title"],
                "summary": item["content"][:120],
            }
            for item in ITEMS.values()
            if query in item["title"].casefold() or query in item["content"].casefold()
        ]
        return {"total": len(hits), "items": hits[:limit]}

    def _read(self, item_id: str) -> dict[str, Any] | None:
        METRICS.increment("read")
        item = ITEMS.get(item_id)
        if item is None:
            return None
        # Redaction proof: the read content echoes the request's Bearer token
        # (simulating knowledge content that contains the configured API
        # credential). DLR must redact it by value before anything reaches
        # the model, the browser or the logs.
        token = os.environ.get("SMOKE_IMA_TOKEN", "")
        return {
            "item": {
                **item,
                "content": f"{item['content']} Configured token: {token}",
            }
        }

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._write_json(200, {"status": "ok"})
            return
        if not self._authorized():
            self._write_json(401, {"error": {"code": "unauthorized", "message": "fake"}})
            return
        if self.path == "/_smoke/metrics":
            self._write_json(200, METRICS.snapshot())
            return
        if self.path.startswith("/v1/knowledge/bases"):
            self._write_json(200, self._bases())
            return
        if self.path.startswith("/v1/knowledge/items/"):
            item_id = self.path[len("/v1/knowledge/items/") :].split("?")[0]
            item = self._read(item_id)
            if item is None:
                self._write_json(404, {"error": {"code": "not_found", "message": "fake"}})
                return
            self._write_json(200, item)
            return
        self._write_json(404, {"error": {"code": "not_found", "message": "fake"}})

    def do_POST(self) -> None:
        if self.path != "/v1/knowledge/search":
            self._write_json(404, {"error": {"code": "not_found", "message": "fake"}})
            return
        if not self._authorized():
            self._write_json(401, {"error": {"code": "unauthorized", "message": "fake"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise TypeError("request body must be an object")
        except (json.JSONDecodeError, TypeError, ValueError):
            self._write_json(400, {"error": {"code": "bad_request", "message": "fake"}})
            return
        self._write_json(200, self._search(payload))

    def log_message(self, format_string: str, *args: object) -> None:
        # Keep smoke logs useful without ever logging headers or bodies.
        print(f"ima-fake: {format_string % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
