"""Isolated Kubernetes reconciliation target, backed by a separate PostgreSQL DB.

This is an acceptance fixture, not a production Sink. It never resets a database.
All target mutations and their idempotent results commit in one scope transaction.
Configuration, source addresses and credentials must remain outside the repository.
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

MAX_BODY_BYTES = 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
KINDS = {"sync.started", "sync.completed", "sync.failed", "object.present", "object.removed"}


class TargetRejected(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status


def initialize(connection: psycopg.Connection[Any]) -> None:
    statements = (
        "CREATE TABLE IF NOT EXISTS dlr_k8s_target_scopes ("
        "subscription_id text NOT NULL, scope_fingerprint text NOT NULL, "
        "source_identity text NOT NULL, cluster_id text NOT NULL, api_group text NOT NULL, "
        "api_version text NOT NULL, resource text NOT NULL, namespace text, "
        "generation bigint NOT NULL DEFAULT 0, PRIMARY KEY(subscription_id,scope_fingerprint), "
        "UNIQUE(subscription_id,source_identity))",
        "CREATE TABLE IF NOT EXISTS dlr_k8s_target_batches ("
        "subscription_id text NOT NULL, scope_fingerprint text NOT NULL, batch_id text NOT NULL, "
        "generation bigint NOT NULL, state text NOT NULL CHECK(state IN "
        "('active','failed','completed')), "
        "PRIMARY KEY(subscription_id,scope_fingerprint,batch_id))",
        "CREATE TABLE IF NOT EXISTS dlr_k8s_target_members ("
        "subscription_id text NOT NULL, scope_fingerprint text NOT NULL, batch_id text NOT NULL, "
        "uid text NOT NULL, payload_hash text NOT NULL, "
        "PRIMARY KEY(subscription_id,scope_fingerprint,batch_id,uid))",
        "CREATE TABLE IF NOT EXISTS dlr_k8s_target_objects ("
        "subscription_id text NOT NULL, scope_fingerprint text NOT NULL, uid text NOT NULL, "
        "namespace text NOT NULL, name text NOT NULL, payload jsonb NOT NULL, "
        "payload_hash text NOT NULL, "
        "generation bigint NOT NULL, writes bigint NOT NULL DEFAULT 1, "
        "PRIMARY KEY(subscription_id,scope_fingerprint,uid))",
        "CREATE TABLE IF NOT EXISTS dlr_k8s_target_events ("
        "subscription_id text NOT NULL, source_event_id text NOT NULL, payload_hash text NOT NULL, "
        "result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), "
        "PRIMARY KEY(subscription_id,source_event_id))",
    )
    for statement in statements:
        connection.execute(statement)


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def validate(envelope: dict[str, Any]) -> tuple[str, str, str, str, dict[str, Any]]:
    try:
        if envelope["schema"] != "dlr.event.v1" or envelope["source"]["type"] != "kubernetes":
            raise ValueError
        sub = envelope["subscription"]["id"]
        ident = envelope["event"]["id"]
        kind = envelope["event"]["kind"]
        attrs = envelope["source"]["attributes"]
        identity = envelope["source"]["identity"]
        scope = identity.removeprefix("kubernetes-scope-v1:")
        if (
            not isinstance(sub, str)
            or not sub.isascii()
            or not sub.isdecimal()
            or not 0 < len(sub) <= 20
            or int(sub) <= 0
            or not HEX.fullmatch(ident)
            or not identity.startswith("kubernetes-scope-v1:")
            or not HEX.fullmatch(scope)
            or kind not in KINDS
            or (attrs["group"], attrs["version"], attrs["resource"])
            not in {("", "v1", "pods"), ("apps", "v1", "deployments")}
            or not isinstance(attrs["cluster_id"], str)
            or not 0 < len(attrs["cluster_id"]) <= 256
            or envelope["data"]["encoding"] != "json"
        ):
            raise ValueError
        sync = envelope["sync"]
        if sync is not None and (
            sync["scope_fingerprint"] != scope
            or isinstance(sync["batch_id"], bool)
            or not isinstance(sync["batch_id"], (str, int))
            or not 0 < len(str(sync["batch_id"])) <= 128
        ):
            raise ValueError
        if kind.startswith("sync."):
            if (
                sync is None
                or sync["phase"] != kind.removeprefix("sync.")
                or envelope["data"]["value"] is not None
                or not isinstance(sync["read_complete"], bool)
                or isinstance(sync["item_count"], bool)
                or not isinstance(sync["item_count"], int)
                or not 0 <= sync["item_count"] <= 5000
            ):
                raise ValueError
        else:
            value = envelope["data"]["value"]
            meta = value["metadata"]
            if (
                not isinstance(attrs["uid"], str)
                or not 0 < len(attrs["uid"]) <= 128
                or not isinstance(attrs["name"], str)
                or not 0 < len(attrs["name"]) <= 253
                or not isinstance(attrs["namespace"], str)
                or not 0 < len(attrs["namespace"]) <= 253
                or meta["uid"] != attrs["uid"]
                or meta["name"] != attrs["name"]
                or meta["namespace"] != attrs["namespace"]
                or meta["resourceVersion"] != envelope["source"]["position"]["resource_version"]
                or attrs["watch_type"] not in {"LIST", "ADDED", "MODIFIED", "DELETED"}
                or (kind == "object.removed") != (attrs["watch_type"] == "DELETED")
                or (attrs["watch_type"] == "LIST" and (sync is None or sync["phase"] != "list"))
                or (attrs["watch_type"] != "LIST" and sync is not None and sync["phase"] != "watch")
            ):
                raise ValueError
        return sub, ident, kind, scope, attrs
    except (KeyError, TypeError, ValueError, AttributeError):
        raise TargetRejected("target_event_invalid", 422) from None


def apply_event(connection: psycopg.Connection[Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """Caller owns the transaction; rejection rolls back all mutations."""
    sub, ident, kind, scope, attrs = validate(envelope)
    sync = envelope["sync"]
    owner = (sub, scope)
    stable = {key: envelope[key] for key in ("event", "source", "data")}
    if kind.startswith("sync.") or attrs["watch_type"] == "LIST":
        stable["sync"] = sync
    digest = hashlib.sha256(canonical(stable)).hexdigest()
    if kind in {"sync.started", "sync.failed"}:
        connection.execute(
            "INSERT INTO dlr_k8s_target_scopes "
            "(subscription_id,scope_fingerprint,source_identity,cluster_id,api_group,api_version,"
            "resource,namespace) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                *owner,
                envelope["source"]["identity"],
                attrs["cluster_id"],
                attrs["group"],
                attrs["version"],
                attrs["resource"],
                attrs["namespace"],
            ),
        )
    scoped = connection.execute(
        "SELECT generation,cluster_id,api_group,api_version,resource,namespace "
        "FROM dlr_k8s_target_scopes WHERE subscription_id=%s AND scope_fingerprint=%s FOR UPDATE",
        owner,
    ).fetchone()
    if scoped is None:
        raise TargetRejected("target_scope_not_started")
    generation, cluster, group, version, resource, namespace = scoped
    if (cluster, group, version, resource) != (
        attrs["cluster_id"],
        attrs["group"],
        attrs["version"],
        attrs["resource"],
    ) or ((kind.startswith("sync.") or namespace is not None) and namespace != attrs["namespace"]):
        raise TargetRejected("target_scope_conflict")
    prior = connection.execute(
        "SELECT payload_hash,result FROM dlr_k8s_target_events "
        "WHERE subscription_id=%s AND source_event_id=%s",
        (sub, ident),
    ).fetchone()
    if prior is not None:
        if prior[0] != digest:
            raise TargetRejected("target_event_identity_conflict")
        return {**prior[1], "applied": False}

    batch_id = str(sync["batch_id"]) if sync is not None else None
    batch = None
    if batch_id is not None:
        batch = connection.execute(
            "SELECT generation,state FROM dlr_k8s_target_batches "
            "WHERE subscription_id=%s AND scope_fingerprint=%s AND batch_id=%s",
            (*owner, batch_id),
        ).fetchone()
    result: dict[str, Any] = {
        "source_event_id": ident,
        "kind": kind,
        "applied": True,
        "generation": generation,
        "cleaned": 0,
    }
    if kind == "sync.started":
        if batch is not None:
            raise TargetRejected("target_batch_not_new")
        generation += 1
        connection.execute(
            "UPDATE dlr_k8s_target_scopes SET generation=%s "
            "WHERE subscription_id=%s AND scope_fingerprint=%s",
            (generation, *owner),
        )
        connection.execute(
            "INSERT INTO dlr_k8s_target_batches VALUES (%s,%s,%s,%s,'active')",
            (*owner, batch_id, generation),
        )
        result["generation"] = generation
    elif kind == "sync.failed":
        if batch is not None and batch[1] == "completed":
            raise TargetRejected("target_batch_already_completed")
        connection.execute(
            "INSERT INTO dlr_k8s_target_batches VALUES (%s,%s,%s,%s,'failed') "
            "ON CONFLICT(subscription_id,scope_fingerprint,batch_id) DO UPDATE SET state='failed'",
            (*owner, batch_id, generation),
        )
    elif kind == "sync.completed":
        if batch is None or batch != (generation, "active") or not sync["read_complete"]:
            raise TargetRejected("target_batch_cleanup_ineligible")
        counts = connection.execute(
            "SELECT count(*), count(o.uid) FILTER (WHERE o.generation=%s "
            "AND o.payload_hash=m.payload_hash) FROM dlr_k8s_target_members m "
            "LEFT JOIN dlr_k8s_target_objects o ON o.subscription_id=m.subscription_id "
            "AND o.scope_fingerprint=m.scope_fingerprint AND o.uid=m.uid "
            "WHERE m.subscription_id=%s AND m.scope_fingerprint=%s AND m.batch_id=%s",
            (generation, *owner, batch_id),
        ).fetchone()
        if counts != (sync["item_count"], sync["item_count"]):
            raise TargetRejected("target_batch_members_incomplete")
        result["cleaned"] = connection.execute(
            "DELETE FROM dlr_k8s_target_objects WHERE subscription_id=%s "
            "AND scope_fingerprint=%s AND generation<%s",
            (*owner, generation),
        ).rowcount
        connection.execute(
            "UPDATE dlr_k8s_target_batches SET state='completed' WHERE subscription_id=%s "
            "AND scope_fingerprint=%s AND batch_id=%s",
            (*owner, batch_id),
        )
    else:
        if (
            attrs["watch_type"] != "LIST"
            and batch_id is not None
            and (batch is None or batch[0] != generation)
        ):
            raise TargetRejected("target_watch_generation_stale")
        if attrs["watch_type"] == "LIST":
            if batch is None or batch[0] != generation or batch[1] not in {"active", "failed"}:
                raise TargetRejected("target_batch_member_ineligible")
            member_hash = hashlib.sha256(canonical(envelope["data"]["value"])).hexdigest()
            connection.execute(
                "INSERT INTO dlr_k8s_target_members VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (*owner, batch_id, attrs["uid"], member_hash),
            )
            old_hash = connection.execute(
                "SELECT payload_hash FROM dlr_k8s_target_members WHERE subscription_id=%s "
                "AND scope_fingerprint=%s AND batch_id=%s AND uid=%s",
                (*owner, batch_id, attrs["uid"]),
            ).fetchone()[0]
            if member_hash != old_hash:
                raise TargetRejected("target_batch_member_conflict")
        if kind == "object.removed":
            result["removed"] = connection.execute(
                "DELETE FROM dlr_k8s_target_objects WHERE subscription_id=%s "
                "AND scope_fingerprint=%s AND uid=%s",
                (*owner, attrs["uid"]),
            ).rowcount
        else:
            connection.execute(
                "INSERT INTO dlr_k8s_target_objects "
                "(subscription_id,scope_fingerprint,uid,namespace,name,payload,payload_hash,"
                "generation) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT "
                "(subscription_id,scope_fingerprint,uid) DO UPDATE SET "
                "namespace=EXCLUDED.namespace, "
                "name=EXCLUDED.name,payload=EXCLUDED.payload,payload_hash=EXCLUDED.payload_hash, "
                "generation=EXCLUDED.generation, "
                "writes=dlr_k8s_target_objects.writes+1",
                (
                    *owner,
                    attrs["uid"],
                    attrs["namespace"],
                    attrs["name"],
                    Jsonb(envelope["data"]["value"]),
                    hashlib.sha256(canonical(envelope["data"]["value"])).hexdigest(),
                    generation,
                ),
            )
    connection.execute(
        "INSERT INTO dlr_k8s_target_events VALUES (%s,%s,%s,%s,DEFAULT)",
        (sub, ident, digest, Jsonb(result)),
    )
    return result


def create_server(config: dict[str, Any]) -> ThreadingHTTPServer:
    dsn, token = config["database_url"], config["token"]
    if not isinstance(token, str) or len(token) < 16:
        raise ValueError("private_target_token_required")
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        initialize(connection)
    lock = threading.Lock()
    fault: dict[str, Any] = {"mode": "normal", "source_event_id": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def answer(self, status: int, value: Any) -> None:
            body = canonical(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.answer(401, {"code": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if self.path == "/health":
                self.answer(200, {"status": "ok", "fixture": "kubernetes-reconciliation"})
                return
            if not self.authorized():
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/events":
                with psycopg.connect(dsn, connect_timeout=5) as connection:
                    connection.execute("SET LOCAL statement_timeout='5s'")
                    row = connection.execute(
                        "SELECT result FROM dlr_k8s_target_events "
                        "WHERE subscription_id=%s AND source_event_id=%s",
                        (
                            query.get("subscription_id", [""])[0],
                            query.get("source_event_id", [""])[0],
                        ),
                    ).fetchone()
                self.answer(200, {"result": row[0] if row else None})
                return
            self.answer(404, {"code": "not_found"})

        def do_POST(self) -> None:
            if not self.authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError
                if self.path == "/fault":
                    mode, ident = data["mode"], data.get("source_event_id")
                    if mode not in {"normal", "unavailable", "lost_response_once"} or (
                        mode != "normal"
                        and (not isinstance(ident, str) or not HEX.fullmatch(ident))
                    ):
                        raise ValueError
                    with lock:
                        fault.update(mode=mode, source_event_id=ident)
                    self.answer(200, {"status": "configured"})
                    return
                if self.path != "/events":
                    self.answer(404, {"code": "not_found"})
                    return
                _, ident, _, _, _ = validate(data)
                if self.headers.get("Idempotency-Key") != ident:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                self.answer(422, {"code": "target_request_invalid"})
                return
            except TargetRejected as error:
                self.answer(error.status, {"code": error.code})
                return
            with lock:
                unavailable = fault["mode"] == "unavailable" and fault["source_event_id"] == ident
            if unavailable:
                self.answer(503, {"code": "injected_target_unavailable"})
                return
            try:
                with psycopg.connect(dsn, connect_timeout=5) as connection:
                    connection.execute("SET LOCAL statement_timeout='5s'")
                    result = apply_event(connection, data)
            except TargetRejected as error:
                self.answer(error.status, {"code": error.code})
                return
            except psycopg.Error:
                self.answer(503, {"code": "target_database_unavailable"})
                return
            # Target transaction has committed before losing this one response.
            with lock:
                lose = fault["mode"] == "lost_response_once" and fault["source_event_id"] == ident
                if lose:
                    fault.update(mode="normal", source_event_id=None)
            if lose:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            self.answer(200, result)

    return ThreadingHTTPServer((config.get("host", "127.0.0.1"), int(config["port"])), Handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    create_server(json.loads(args.config.read_text())).serve_forever()
