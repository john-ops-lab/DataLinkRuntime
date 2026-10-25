"""Isolated PostgreSQL-backed acceptance target; private configuration is mandatory.

Not a production ingestion service. Never resets or drops a database. Business
effects use a primary key and content hash; faults occur after the target commit.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psycopg
from psycopg.types.json import Jsonb

BUSINESS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
MAX_BODY_BYTES = 512 * 1024


def serve(config: dict[str, Any]) -> None:
    dsn, token = config["database_url"], config["token"]
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("private target token must contain at least 16 characters")
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS dlr_subscription_target ("
            "business_id text PRIMARY KEY, payload jsonb NOT NULL, payload_hash text NOT NULL, "
            "source_event_id text NOT NULL, writes integer NOT NULL DEFAULT 1 CHECK(writes=1), "
            "created_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
    fault_lock = threading.Lock()
    faults: dict[str, Any] = {"mode": "normal", "business_id": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            # No request path, Authorization, payload, DSN or traceback logging.
            return

        def answer(self, status: int, data: Any) -> None:
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def authorized(self) -> bool:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.answer(401, {"code": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if self.path == "/health":
                self.answer(200, {"status": "ok"})
                return
            if not self.authorized():
                return
            path = urlsplit(self.path)
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                connection.execute("SET LOCAL statement_timeout='5s'")
                if path.path == "/stats":
                    row = connection.execute(
                        "SELECT count(*), coalesce(sum(writes),0) FROM dlr_subscription_target"
                    ).fetchone()
                    assert row is not None
                    self.answer(200, {"records": row[0], "effective_writes": row[1]})
                    return
                if path.path == "/records":
                    ident = parse_qs(path.query).get("business_id", [""])[0]
                    rows = connection.execute(
                        "SELECT business_id,payload,source_event_id,writes "
                        "FROM dlr_subscription_target WHERE business_id=%s LIMIT 1",
                        (ident,),
                    ).fetchall()
                    self.answer(
                        200,
                        {
                            "items": [
                                dict(
                                    zip(
                                        ("business_id", "payload", "source_event_id", "writes"),
                                        row,
                                        strict=True,
                                    )
                                )
                                for row in rows
                            ]
                        },
                    )
                    return
            self.answer(404, {"code": "not_found"})

        def do_POST(self) -> None:
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError("size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("object required")
                if self.path == "/fault":
                    if data.get("mode") not in {"normal", "unavailable", "lost_response_once"}:
                        raise ValueError("mode")
                    with fault_lock:
                        faults.update(mode=data["mode"], business_id=data.get("business_id"))
                    self.answer(200, {"status": "configured"})
                    return
                if self.path != "/events":
                    self.answer(404, {"code": "not_found"})
                    return
                if set(data) != {"business_id", "payload", "source_event_id"}:
                    raise ValueError("shape")
                ident = data["business_id"]
                if (
                    not isinstance(ident, str)
                    or BUSINESS_ID.fullmatch(ident) is None
                    or self.headers.get("Idempotency-Key") != ident
                    or not isinstance(data["payload"], dict)
                    or not isinstance(data["source_event_id"], str)
                    or len(data["source_event_id"]) > 128
                ):
                    raise ValueError("identity")
                payload_hash = hashlib.sha256(
                    json.dumps(
                        data["payload"], sort_keys=True, ensure_ascii=False, allow_nan=False
                    ).encode()
                ).hexdigest()
            except (ValueError, TypeError, KeyError):
                self.answer(422, {"code": "request_invalid"})
                return
            with fault_lock:
                unavailable = faults["mode"] == "unavailable" and (
                    faults["business_id"] is None or faults["business_id"] == ident
                )
            if unavailable:
                self.answer(503, {"code": "injected_target_unavailable"})
                return
            try:
                with psycopg.connect(dsn, connect_timeout=5) as connection:
                    connection.execute("SET LOCAL statement_timeout='5s'")
                    row = connection.execute(
                        "INSERT INTO dlr_subscription_target "
                        "(business_id,payload,payload_hash,source_event_id) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT (business_id) DO NOTHING RETURNING business_id",
                        (ident, Jsonb(data["payload"]), payload_hash, data["source_event_id"]),
                    ).fetchone()
                    applied = row is not None
                    if not applied:
                        old = connection.execute(
                            "SELECT payload_hash FROM dlr_subscription_target WHERE business_id=%s",
                            (ident,),
                        ).fetchone()
                        if old is None or old[0] != payload_hash:
                            self.answer(409, {"code": "business_identity_conflict"})
                            return
            except psycopg.Error:
                self.answer(503, {"code": "target_database_unavailable"})
                return
            # The database context committed before this intentional transport failure.
            with fault_lock:
                lose = faults["mode"] == "lost_response_once" and (
                    faults["business_id"] is None or faults["business_id"] == ident
                )
                if lose:
                    faults.update(mode="normal", business_id=None)
            if lose:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            self.answer(200, {"business_id": ident, "applied": applied})

    server = ThreadingHTTPServer((config.get("host", "127.0.0.1"), int(config["port"])), Handler)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    serve(json.loads(args.config.read_text()))
