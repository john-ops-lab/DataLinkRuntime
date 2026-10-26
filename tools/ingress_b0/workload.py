#!/usr/bin/env python3
"""Bounded B0 fixtures; live endpoints and evidence belong in private JSON.

Usage: workload.py {generate,prepare-target,publish,verify} --config PRIVATE.json
Required common keys: run_id, input_path, output_path (an evidence directory).
Formal input requires count=200; warmup=true requires count=20. Target commands need target_dsn;
publish
needs amqp_url and queue. Dependencies are imported only by live commands.
No command deletes or truncates a table/queue, retries publication, or declares
DLR acceptance from publisher confirms. Existing evidence is never overwritten.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BODY_BYTES = 2048
WINDOW = 32
MAX_ATTEMPTS = 64
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
PUBLISH_LOG_MAX_BYTES = 512 * 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
RESULT_TABLE = "dlr_ingress_b0_results"
ATTEMPT_TABLE = "dlr_ingress_b0_attempts"


class FixtureError(Exception):
    """Only stable, non-sensitive fixture codes reach the CLI."""


def utc() -> str:
    return datetime.now(UTC).isoformat()


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()


def _private_path(path: str | Path) -> Path:
    result = Path(path).expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    if result == repository or repository in result.parents:
        raise FixtureError("evidence_must_be_outside_repository")
    return result


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


class PrivateLog:
    def __init__(self, path: Path, *, max_bytes: int = MAX_EVIDENCE_BYTES) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EVIDENCE_BYTES:
            raise FixtureError("invalid_evidence_limit")
        _mkdir(path.parent)
        self.handle = os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        )
        self.size = 0
        self.max_bytes = max_bytes

    def append(self, value: Any) -> None:
        raw = _encoded(value) + b"\n"
        if self.size + len(raw) > self.max_bytes:
            raise FixtureError("evidence_limit_reached")
        self.handle.write(raw)
        self.handle.flush()
        self.size += len(raw)

    def close(self) -> None:
        self.handle.close()


def write_result(config: dict[str, Any], stage: str, value: Any) -> None:
    output = _private_path(config["output_path"])
    log = PrivateLog(output / f"{config['run_id']}-{stage}.private.json")
    try:
        log.append(value)
    finally:
        log.close()


def load_config(path: Path) -> dict[str, Any]:
    path = _private_path(path)
    if path.stat().st_size > 65536:
        raise FixtureError("configuration_too_large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not RUN_ID.fullmatch(value.get("run_id", "")):
        raise FixtureError("invalid_run_id")
    for name in ("input_path", "output_path"):
        value[name] = str(_private_path(value[name]))
    return value


def input_record(run_id: str, sequence: int, *, warmup: bool = False) -> dict[str, Any]:
    if not RUN_ID.fullmatch(run_id) or not 1 <= sequence <= 200:
        raise FixtureError("invalid_input_identity")
    message_id = f"{'w' if warmup else ''}{sequence:06d}"
    value = {
        "run_id": run_id,
        "message_id": message_id,
        "seq": sequence,
        "value": sequence,
        "padding": "",
    }
    remaining = BODY_BYTES - len(_encoded(value))
    if remaining < 0:
        raise FixtureError("input_exceeds_fixed_size")
    # Deterministic ASCII, with less compression bias than one repeated byte.
    padding = "".join(
        hashlib.sha256(f"{run_id}:{sequence}:{i}".encode()).hexdigest()
        for i in range((remaining + 63) // 64)
    )
    value["padding"] = padding[:remaining]
    raw = _encoded(value)
    if len(raw) != BODY_BYTES:
        raise FixtureError("input_size_mismatch")
    return {
        "schema": "dlr.ingress-b0.input.v1",
        "run_id": run_id,
        "message_id": message_id,
        "seq": sequence,
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_size": len(raw),
        "expected_normalized_value": sequence + sequence + 1,
    }


def workload_count(config: dict[str, Any]) -> int:
    warmup = config.get("warmup", False)
    if type(warmup) is not bool:
        raise FixtureError("warmup_must_be_boolean")
    expected = 20 if warmup else 200
    if (
        type(config.get("count", expected)) is not int
        or config.get("count", expected) != expected
    ):
        raise FixtureError("workload_count_mismatch")
    return expected


def generate(config: dict[str, Any]) -> dict[str, Any]:
    count = workload_count(config)
    warmup = config.get("warmup", False)
    log = PrivateLog(_private_path(config["input_path"]))
    try:
        for sequence in range(1, count + 1):
            log.append(input_record(config["run_id"], sequence, warmup=warmup))
    finally:
        log.close()
    return {
        "stage": "generate",
        "run_id": config["run_id"],
        "count": count,
        "warmup": warmup,
        "body_bytes": BODY_BYTES,
        "manifest_sha256": hashlib.sha256(
            Path(config["input_path"]).read_bytes()
        ).hexdigest(),
    }


def read_inputs(config: dict[str, Any]) -> list[dict[str, Any]]:
    count = workload_count(config)
    path = _private_path(config["input_path"])
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise FixtureError("manifest_too_large")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    if len(rows) != count:
        raise FixtureError("invalid_manifest_count")
    for sequence, row in enumerate(rows, 1):
        if row != input_record(
            config["run_id"], sequence, warmup=config.get("warmup", False)
        ):
            raise FixtureError("manifest_content_mismatch")
    return rows


def prepare_target(config: dict[str, Any]) -> dict[str, Any]:
    import psycopg

    with psycopg.connect(config["target_dsn"], connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout='5s'")
        connection.execute(f"""CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
          run_id varchar(80) NOT NULL, message_id varchar(16) NOT NULL,
          seq integer NOT NULL, raw_sha256 char(64) NOT NULL,
          normalized_value bigint NOT NULL, writes integer NOT NULL DEFAULT 1 CHECK(writes=1),
          attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 64),
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(run_id,message_id))""")
        connection.execute(f"""CREATE TABLE IF NOT EXISTS {ATTEMPT_TABLE} (
          run_id varchar(80) NOT NULL, message_id varchar(16) NOT NULL,
          ordinal integer NOT NULL CHECK(ordinal BETWEEN 1 AND 64),
          outcome varchar(16) NOT NULL CHECK(outcome IN ('inserted','duplicate')),
          raw_sha256 char(64) NOT NULL, normalized_value bigint NOT NULL,
          handle_started_at timestamptz NOT NULL, connected_at timestamptz NOT NULL,
          recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY(run_id,message_id,ordinal),
          FOREIGN KEY(run_id,message_id) REFERENCES {RESULT_TABLE}(run_id,message_id))""")
        synchronous = connection.execute("SHOW synchronous_commit").fetchone()[0]
        if synchronous != "on":
            raise FixtureError("target_requires_synchronous_commit_on")
    return {
        "stage": "prepare-target",
        "prepared": True,
        "synchronous_commit": synchronous,
        "destructive_operations": False,
    }


def verify_rows(
    inputs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Independent target oracle; does not import or call Adapter code."""
    expected = {(x["run_id"], x["message_id"]): x for x in inputs}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    attempt_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    for row in rows:
        grouped.setdefault((row["run_id"], row["message_id"]), []).append(row)
    for row in attempts:
        attempt_groups.setdefault((row["run_id"], row["message_id"]), []).append(row)
    for key in set(grouped) | set(attempt_groups):
        if key not in expected:
            failures.append({"message_id": "unexpected", "code": "unexpected_identity"})
    for key, wanted in expected.items():
        found = grouped.get(key, [])
        if len(found) != 1:
            failures.append(
                {"message_id": key[1], "code": "missing_or_duplicate_result"}
            )
            continue
        row = found[0]
        if (
            row["seq"] != wanted["seq"]
            or row["raw_sha256"] != wanted["raw_sha256"]
            or row["normalized_value"] != wanted["expected_normalized_value"]
        ):
            failures.append({"message_id": key[1], "code": "target_content_mismatch"})
        if row["writes"] != 1:
            failures.append({"message_id": key[1], "code": "duplicate_effect"})
        seen = attempt_groups.get(key, [])
        if (
            not 1 <= row["attempts"] <= MAX_ATTEMPTS
            or len(seen) != row["attempts"]
            or sorted(x["ordinal"] for x in seen) != list(range(1, len(seen) + 1))
        ):
            failures.append(
                {"message_id": key[1], "code": "attempt_accounting_mismatch"}
            )
        if sum(x["outcome"] == "inserted" for x in seen) != 1:
            failures.append(
                {"message_id": key[1], "code": "insert_effect_accounting_mismatch"}
            )
        if any(
            x["outcome"] not in {"inserted", "duplicate"}
            or x["raw_sha256"] != wanted["raw_sha256"]
            or x["normalized_value"] != wanted["expected_normalized_value"]
            for x in seen
        ):
            failures.append({"message_id": key[1], "code": "attempt_content_mismatch"})
    return {
        "stage": "verify",
        "run_id": inputs[0]["run_id"],
        "expected_count": len(inputs),
        "target_rows": len(rows),
        "recorded_attempts": len(attempts),
        "effective_writes": sum(row["writes"] for row in rows),
        "target_oracle_passed": not failures,
        "failures": failures,
        "scope": "independent target only; source and DLR custody still require reconciliation",
    }


def verify(config: dict[str, Any]) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    inputs = read_inputs(config)
    with psycopg.connect(
        config["target_dsn"], connect_timeout=5, row_factory=dict_row
    ) as connection:
        connection.execute("SET LOCAL statement_timeout='5s'")
        rows = connection.execute(
            f"SELECT * FROM {RESULT_TABLE} WHERE run_id=%s ORDER BY message_id LIMIT %s",
            (config["run_id"], len(inputs) + 1),
        ).fetchall()
        attempts = connection.execute(
            f"SELECT * FROM {ATTEMPT_TABLE} WHERE run_id=%s ORDER BY message_id,ordinal LIMIT %s",
            (config["run_id"], len(inputs) * MAX_ATTEMPTS + 1),
        ).fetchall()
    return verify_rows(inputs, rows, attempts)


class Publisher:
    """One channel, mandatory routing and an explicit <=32 confirm window.

    Pika 1.3.2's SelectConnection/Channel confirmation callbacks are used; no
    automatic reconnect/republication changes the original publishing facts.
    """

    def __init__(
        self, config: dict[str, Any], inputs: list[dict[str, Any]], sink: PrivateLog
    ) -> None:
        self.config, self.inputs, self.sink = config, inputs, sink
        self.connection: Any = None
        self.channel: Any = None
        self.pika: Any = None
        self.next_index = 0
        self.pending: dict[int, dict[str, Any]] = {}
        self.acked: set[int] = set()
        self.nacked: set[int] = set()
        self.returned: set[str] = set()
        self.first_at: float | None = None
        self.last_confirm_at: float | None = None
        self.failure: str | None = None
        self.complete = False
        self.closing = False
        self.window_peak = 0

    def event(self, kind: str, **fields: Any) -> None:
        self.sink.append(
            {"kind": kind, "utc": utc(), "monotonic_ns": time.monotonic_ns(), **fields}
        )

    def fail(self, code: str) -> None:
        if self.failure is None:
            self.failure = code
            self.event("publisher_failure", code=code)
        self.close()

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.connection is not None:
            self.connection.ioloop.call_later(2, self.connection.ioloop.stop)
            if self.connection.is_open:
                self.connection.close()
            else:
                self.connection.ioloop.stop()

    def on_open(self, connection: Any) -> None:
        connection.channel(on_open_callback=self.on_channel)

    def on_open_error(self, _connection: Any, _error: Any) -> None:
        self.fail("connection_open_failed")

    def on_closed(self, connection: Any, _reason: Any) -> None:
        if not self.complete and self.failure is None:
            self.failure = "connection_closed_before_all_confirms"
            self.event("publisher_failure", code=self.failure)
        connection.ioloop.stop()

    def on_channel_closed(self, _channel: Any, _reason: Any) -> None:
        if not self.complete:
            self.fail("channel_closed_before_all_confirms")

    def on_channel(self, channel: Any) -> None:
        self.channel = channel
        channel.add_on_close_callback(self.on_channel_closed)
        channel.add_on_return_callback(self.on_return)
        channel.queue_declare(
            queue=self.config["queue"],
            durable=True,
            exclusive=False,
            auto_delete=False,
            arguments={
                "x-max-length": 1000,
                "x-max-length-bytes": 16 * 1024 * 1024,
                "x-overflow": "reject-publish",
            },
            callback=self.on_queue_ready,
        )

    def on_queue_ready(self, _frame: Any) -> None:
        self.channel.confirm_delivery(
            self.on_confirmation, callback=self.on_confirm_ready
        )

    def on_confirm_ready(self, _frame: Any) -> None:
        self.pump()

    def pump(self) -> None:
        while (
            not self.closing
            and len(self.pending) < WINDOW
            and self.next_index < len(self.inputs)
        ):
            row = self.inputs[self.next_index]
            tag = self.next_index + 1
            self.pending[tag] = row
            self.next_index += 1
            self.window_peak = max(self.window_peak, len(self.pending))
            started = time.monotonic()
            if self.first_at is None:
                self.first_at = started
            self.event(
                "publish_started", message_id=row["message_id"], delivery_tag=tag
            )
            try:
                self.channel.basic_publish(
                    exchange="",
                    routing_key=self.config["queue"],
                    body=base64.b64decode(row["raw_base64"], validate=True),
                    mandatory=True,
                    properties=self.pika.BasicProperties(
                        message_id=row["message_id"],
                        content_type="application/json",
                        delivery_mode=2,
                    ),
                )
            except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
                self.fail("publish_call_failed_result_unknown")
                return

    def on_return(
        self, _channel: Any, method: Any, properties: Any, _body: Any
    ) -> None:
        ident = properties.message_id
        if ident not in {row["message_id"] for row in self.inputs}:
            self.fail("return_identity_unrecognized")
            return
        self.returned.add(ident)
        self.event(
            "mandatory_return", message_id=ident, reply_code=int(method.reply_code)
        )
        self.fail("mandatory_return")

    def on_confirmation(self, frame: Any) -> None:
        method = frame.method
        tag = int(method.delivery_tag)
        ack = method.NAME == "Basic.Ack"
        if method.NAME not in {"Basic.Ack", "Basic.Nack"}:
            self.fail("invalid_confirmation_type")
            return
        tags = (
            [x for x in self.pending if tag == 0 or x <= tag]
            if method.multiple
            else [tag]
        )
        if not tags or any(x not in self.pending for x in tags):
            self.fail("confirmation_identity_unrecognized")
            return
        for item in tags:
            row = self.pending.pop(item)
            (self.acked if ack else self.nacked).add(item)
            self.event(
                "broker_ack" if ack else "broker_nack",
                message_id=row["message_id"],
                delivery_tag=item,
                multiple=bool(method.multiple),
            )
        self.last_confirm_at = time.monotonic()
        if not ack:
            self.fail("broker_nack")
            return
        if (
            len(self.acked) == len(self.inputs)
            and not self.returned
            and not self.failure
        ):
            self.complete = True
            self.close()
        elif not self.closing:
            self.pump()

    def run(self) -> dict[str, Any]:
        import pika

        if pika.__version__ != "1.3.2":
            raise FixtureError("pika_version_must_be_1_3_2")
        self.pika = pika
        logging.getLogger("pika").addHandler(logging.NullHandler())
        logging.getLogger("pika").propagate = False
        parameters = pika.URLParameters(self.config["amqp_url"])
        parameters.socket_timeout = 5
        parameters.stack_timeout = 10
        parameters.connection_attempts = 1
        parameters.blocked_connection_timeout = 5
        parameters.heartbeat = 30
        self.connection = pika.SelectConnection(
            parameters=parameters,
            on_open_callback=self.on_open,
            on_open_error_callback=self.on_open_error,
            on_close_callback=self.on_closed,
        )
        self.connection.ioloop.call_later(
            30, lambda: self.fail("publisher_total_deadline")
        )
        try:
            self.connection.ioloop.start()
        except Exception:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            if self.failure is None:
                self.failure = "publisher_callback_failed"
                self.event("publisher_failure", code=self.failure)
        finally:
            # IOLoop owns these sockets; close it even after a forced deadline.
            self.connection.ioloop.close()
        for tag, row in self.pending.items():
            self.event(
                "publish_unknown", message_id=row["message_id"], delivery_tag=tag
            )
        for row in self.inputs[self.next_index :]:
            self.event("not_published", message_id=row["message_id"])
        duration = (
            self.last_confirm_at - self.first_at
            if self.last_confirm_at is not None and self.first_at is not None
            else None
        )
        return {
            "stage": "publish",
            "run_id": self.config["run_id"],
            "count": len(self.inputs),
            "attempted": self.next_index,
            "broker_acked": len(self.acked),
            "broker_nacked": len(self.nacked),
            "returned": len(self.returned),
            "unknown": len(self.pending),
            "not_published": len(self.inputs) - self.next_index,
            "window_peak": self.window_peak,
            "max_confirm_window": WINDOW,
            "first_publish_to_last_confirm_seconds": duration,
            "failure_code": self.failure,
            "all_broker_confirmed": self.complete,
            "burst_window_passed": self.complete
            and duration is not None
            and duration <= 5,
            "dlr_acceptance_verified": False,
        }


def publish(config: dict[str, Any]) -> dict[str, Any]:
    queue = config["queue"]
    if (
        not isinstance(queue, str)
        or not 1 <= len(queue.encode()) <= 255
        or queue.startswith("amq.")
        or any(ord(c) < 32 for c in queue)
    ):
        raise FixtureError("invalid_dedicated_queue")
    inputs = read_inputs(config)
    sink = PrivateLog(
        _private_path(config["output_path"])
        / f"{config['run_id']}-publish.private.jsonl",
        max_bytes=PUBLISH_LOG_MAX_BYTES,
    )
    try:
        return Publisher(config, inputs, sink).run()
    finally:
        sink.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("generate", "prepare-target", "publish", "verify")
    )
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result = {
            "generate": generate,
            "prepare-target": prepare_target,
            "publish": publish,
            "verify": verify,
        }[args.command](config)
        write_result(config, args.command, result)
        print(json.dumps(result, default=str, ensure_ascii=False, allow_nan=False))
        return (
            2
            if (
                result.get("target_oracle_passed") is False
                or result.get("burst_window_passed") is False
                or result.get("failure_code") is not None
            )
            else 0
        )
    except Exception as error:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
        # Never print database/SDK messages, connection URLs, bodies or tracebacks.
        code = (
            str(error) if isinstance(error, FixtureError) else "fixture_command_failed"
        )
        print(
            json.dumps(
                {
                    "stage": args.command,
                    "failed": True,
                    "code": code,
                    "error_type": type(error).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
