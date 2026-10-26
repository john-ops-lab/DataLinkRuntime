#!/usr/bin/env python3
"""Bounded ingress fixtures; live endpoints and evidence belong in private JSON.

Usage: workload.py {generate,prepare-target,publish,http,verify} --config PRIVATE.json
Required common keys: run_id, input_path, output_path (an evidence directory).
Explicit kind/count: warmup=20, baseline=200, capacity=10000, fault20=20, fault1000=1000. Target commands need target_dsn;
publish
needs amqp_url and queue. Dependencies are imported only by live commands.
No command deletes/truncates a table or queue. RabbitMQ publication is not retried;
HTTP retries retain the original identity/body within hard budgets. Publisher/HTTP
responses never declare DLR business or capacity acceptance. Existing evidence is never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BODY_BYTES = 2048
WINDOW = 32
KINDS = {
    "warmup": 20,
    "baseline": 200,
    "capacity": 10000,
    "fault20": 20,
    "fault1000": 1000,
}
MANIFEST_MAX_BYTES = 40 * 1024 * 1024
HTTP_LOG_MAX_BYTES = 64 * 1024 * 1024
RESULT_MAX_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 64
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
PUBLISH_LOG_MAX_BYTES = 8 * 1024 * 1024
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
RESULT_TABLE = "dlr_ingress_results"
ATTEMPT_TABLE = "dlr_ingress_attempts"


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
    def __init__(self, path: Path, *, max_bytes: int = RESULT_MAX_BYTES) -> None:
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


def workload_count(config: dict[str, Any]) -> int:
    kind = config.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise FixtureError("invalid_workload_kind")
    if type(config.get("count")) is not int or config["count"] != KINDS[kind]:
        raise FixtureError("workload_count_mismatch")
    if "warmup" in config:
        raise FixtureError("use_explicit_kind_not_warmup_flag")
    return KINDS[kind]


def input_record(
    run_id: str, sequence: int, *, kind: str = "baseline"
) -> dict[str, Any]:
    if (
        not RUN_ID.fullmatch(run_id)
        or kind not in KINDS
        or not 1 <= sequence <= KINDS[kind]
    ):
        raise FixtureError("invalid_input_identity")
    # Include the run identity in the source ID so warmup/formal/fault rounds
    # cannot collide even if they deliberately share a subscription epoch.
    message_id = f"{run_id}:{sequence:06d}"
    value = {
        "run_id": run_id,
        "message_id": message_id,
        "seq": sequence,
        "value": sequence,
        "padding": "",
    }
    remaining = BODY_BYTES - len(_encoded(value))
    padding = "".join(
        hashlib.sha256(f"{run_id}:{sequence}:{i}".encode()).hexdigest()
        for i in range((remaining + 63) // 64)
    )
    value["padding"] = padding[:remaining]
    raw = _encoded(value)
    if len(raw) != BODY_BYTES:
        raise FixtureError("input_size_mismatch")
    canonical = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    return {
        "schema": "dlr.ingress.input.v1",
        "kind": kind,
        "run_id": run_id,
        "message_id": message_id,
        "seq": sequence,
        "raw_base64": base64.b64encode(raw).decode("ascii"),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "payload_sha256": hashlib.sha256(canonical).hexdigest(),
        "raw_size": len(raw),
        "expected_normalized_value": sequence * 2 + 1,
    }


def generate(config: dict[str, Any]) -> dict[str, Any]:
    count = workload_count(config)
    log = PrivateLog(_private_path(config["input_path"]), max_bytes=MANIFEST_MAX_BYTES)
    digest = hashlib.sha256()
    try:
        for sequence in range(1, count + 1):
            row = input_record(config["run_id"], sequence, kind=config["kind"])
            log.append(row)
            digest.update(_encoded(row) + b"\n")
    finally:
        log.close()
    return {
        "stage": "generate",
        "run_id": config["run_id"],
        "count": count,
        "kind": config["kind"],
        "body_bytes": BODY_BYTES,
        "manifest_sha256": digest.hexdigest(),
        "manifest_limit_bytes": MANIFEST_MAX_BYTES,
        "rabbit_log_limit_bytes": PUBLISH_LOG_MAX_BYTES,
        "http_log_limit_bytes": HTTP_LOG_MAX_BYTES,
        "result_limit_bytes": RESULT_MAX_BYTES,
    }


def read_inputs(config: dict[str, Any]) -> list[dict[str, Any]]:
    count = workload_count(config)
    path = _private_path(config["input_path"])
    if path.stat().st_size > MANIFEST_MAX_BYTES:
        raise FixtureError("manifest_too_large")
    rows = []
    with path.open() as source:
        for sequence, line in enumerate(source, 1):
            if sequence > count or not line.strip():
                raise FixtureError("invalid_manifest_count")
            row = json.loads(line)
            if row != input_record(config["run_id"], sequence, kind=config["kind"]):
                raise FixtureError("manifest_content_mismatch")
            rows.append(row)
    if len(rows) != count:
        raise FixtureError("invalid_manifest_count")
    return rows


def prepare_target(config: dict[str, Any]) -> dict[str, Any]:
    import psycopg

    ddl = Path(__file__).with_name("target.sql").read_text()
    with psycopg.connect(config["target_dsn"], connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout='5s'")
        connection.execute(ddl, prepare=False)
        synchronous = connection.execute("SHOW synchronous_commit").fetchone()[0]
        if synchronous != "on":
            raise FixtureError("target_requires_synchronous_commit_on")
    return {
        "stage": "prepare-target",
        "prepared": True,
        "synchronous_commit": synchronous,
        "ddl_sha256": hashlib.sha256(ddl.encode()).hexdigest(),
        "destructive_operations": False,
    }


def verify_rows(
    inputs: list[dict[str, Any]], rows: Any, attempts: Any
) -> dict[str, Any]:
    """Independent streaming target oracle, without importing reference Adapter code."""
    expected = {(x["run_id"], x["message_id"]): x for x in inputs}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    failure_counts: dict[str, int] = {}
    examples: list[dict[str, str]] = []
    target_count = attempt_count = writes = 0

    def fail(identity: str, code: str) -> None:
        failure_counts[code] = failure_counts.get(code, 0) + 1
        if len(examples) < 100:
            examples.append({"message_id": identity, "code": code})

    for row in rows:
        target_count += 1
        key = row["run_id"], row["message_id"]
        wanted = expected.get(key)
        if key in found:
            fail(key[1], "duplicate_result")
        if wanted is None:
            fail("unexpected", "unexpected_identity")
            continue
        found[key] = row
        writes += row["writes"]
        if (
            row["seq"] != wanted["seq"]
            or row["payload_sha256"] != wanted["payload_sha256"]
            or row["normalized_value"] != wanted["expected_normalized_value"]
        ):
            fail(key[1], "target_content_mismatch")
        if row["writes"] != 1:
            fail(key[1], "duplicate_effect")
    for row in attempts:
        attempt_count += 1
        key = row["run_id"], row["message_id"]
        wanted = expected.get(key)
        if wanted is None:
            fail("unexpected", "unexpected_attempt_identity")
            continue
        group = counts.setdefault(key, {"count": 0, "ordinals": set(), "inserted": 0})
        group["count"] += 1
        group["ordinals"].add(row["ordinal"])
        group["inserted"] += row["outcome"] == "inserted"
        if (
            row["outcome"] not in {"inserted", "duplicate"}
            or row["payload_sha256"] != wanted["payload_sha256"]
            or row["normalized_value"] != wanted["expected_normalized_value"]
        ):
            fail(key[1], "attempt_content_mismatch")
    for key in expected:
        if key not in found:
            fail(key[1], "missing_result")
            continue
        total = found[key]["attempts"]
        group = counts.get(key, {"count": 0, "ordinals": set(), "inserted": 0})
        if (
            not 1 <= total <= MAX_ATTEMPTS
            or group["count"] != total
            or group["ordinals"] != set(range(1, total + 1))
        ):
            fail(key[1], "attempt_accounting_mismatch")
        if group["inserted"] != 1:
            fail(key[1], "insert_effect_accounting_mismatch")
    return {
        "stage": "verify",
        "run_id": inputs[0]["run_id"],
        "expected_count": len(inputs),
        "target_rows": target_count,
        "recorded_attempts": attempt_count,
        "effective_writes": writes,
        "target_oracle_passed": not failure_counts,
        "failure_counts": failure_counts,
        "failure_examples": examples,
        "scope": "independent target only; source and DLR custody require separate reconciliation",
    }


def verify(config: dict[str, Any]) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    inputs = read_inputs(config)
    with psycopg.connect(
        config["target_dsn"], connect_timeout=5, row_factory=dict_row
    ) as connection:
        connection.execute("SET LOCAL statement_timeout='30s'")
        with (
            connection.cursor(name="b4_results") as rows,
            connection.cursor(name="b4_attempts") as attempts,
        ):
            rows.execute(
                f"SELECT * FROM {RESULT_TABLE} WHERE run_id=%s ORDER BY message_id LIMIT %s",
                (config["run_id"], len(inputs) + 1),
            )
            attempts.execute(
                f"SELECT * FROM {ATTEMPT_TABLE} WHERE run_id=%s ORDER BY message_id,ordinal LIMIT %s",
                (config["run_id"], len(inputs) * MAX_ATTEMPTS + 1),
            )
            rows.itersize = attempts.itersize = 1000
            return verify_rows(inputs, rows, attempts)


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes = b""
    retry_after: str | None = None


def retry_after_seconds(value: str | None) -> tuple[int, bool]:
    """Only the contract's integer 1..30 seconds is valid, never wall-clock math."""
    if value is not None and value.isascii() and value.isdigit():
        number = int(value)
        if 1 <= number <= 30:
            return number, True
    return 1, False


class HttpSender:
    """Finite per-ID retries; the caller supplies transport and a private event sink."""

    def __init__(
        self, config, inputs, sink, send, *, clock=time.monotonic, sleep=asyncio.sleep
    ):
        self.config, self.inputs, self.sink, self.send = config, inputs, sink, send
        self.clock, self.sleep = clock, sleep
        concurrency = config.get("http_concurrency", 128)
        if type(concurrency) is not int or not 1 <= concurrency <= 128:
            raise FixtureError("invalid_http_concurrency")
        self.slots = asyncio.Semaphore(concurrency)
        self.first_round_done = asyncio.Event()
        self.first_accounted: set[int] = set()
        self.start = self.clock()
        self.deadline = self.start + 60
        self.first_deadline = self.start + 5
        self.active = self.peak = 0
        self.counts: Counter[str] = Counter(
            {
                key: 0
                for key in (
                    "post_requests",
                    "query_requests",
                    "accepted",
                    "rejected",
                    "unknown",
                    "permanent_rejection",
                    "invalid_retry_after",
                    "first_not_sent",
                    "first_accepted",
                    "first_rejected",
                    "first_unknown",
                    "first_permanent_rejection",
                )
            }
        )
        self.statuses: Counter[str] = Counter()
        self.query_statuses: Counter[str] = Counter()
        self.first_statuses: Counter[str] = Counter()
        self.first_starts: list[float] = []
        self.last_accepted_at: float | None = None

    def _mark_first(self, row, started=None):
        if row["seq"] in self.first_accounted:
            return
        self.first_accounted.add(row["seq"])
        if started is None:
            self.counts["first_not_sent"] += 1
        else:
            self.first_starts.append(started)
        if len(self.first_accounted) == len(self.inputs):
            self.first_round_done.set()

    async def _request(self, method, row, receipt_id=None, *, first=False, timing=None):
        # Waiting for the first round or backoff never consumes an in-flight
        # HTTP slot. Queries and retries cannot overtake any first POST.
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError
        if not first:
            await asyncio.wait_for(self.first_round_done.wait(), timeout=remaining)
        deadline = min(self.deadline, self.first_deadline) if first else self.deadline
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(self.slots.acquire(), timeout=remaining)
        try:
            started = self.clock()
            if started >= deadline:
                raise TimeoutError
            if first:
                self._mark_first(row, started)
            if timing is not None:
                timing["started"] = started
            self.counts["post_requests" if method == "POST" else "query_requests"] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                return await asyncio.wait_for(
                    self.send(method, row, receipt_id),
                    timeout=min(5.0, self.deadline - started),
                )
            finally:
                self.active -= 1
                if timing is not None:
                    timing["ended"] = self.clock()
        finally:
            self.slots.release()

    def _receipt(self, reply):
        if reply.status not in {200, 202}:
            return None, False
        try:
            value = json.loads(reply.body)
        except (ValueError, UnicodeError):
            return None, False
        if not isinstance(value, dict) or type(value.get("receipt_id")) is not int:
            return None, False
        identity = value["receipt_id"]
        if identity < 1:
            return None, False
        route = urlsplit(self.config["ingress_url"]).path
        valid_status = value.get("processing_status") in {
            "pending",
            "processing",
            "succeeded",
            "failed",
            "skipped",
            "superseded",
            "abandoned",
        }
        return identity, valid_status and value.get(
            "status_url"
        ) == f"{route}/receipts/{identity}"

    async def _one(self, row):
        count = 0
        for attempt in range(1, 21):
            if self.clock() >= (self.first_deadline if attempt == 1 else self.deadline):
                if attempt == 1:
                    self._mark_first(row)
                break
            delay = 1
            identity = None
            retry = False
            status = 0
            post_timing, query_timing = {}, {}
            query_status = 0
            invalid_retry_after = False
            try:
                reply = await self._request(
                    "POST", row, first=attempt == 1, timing=post_timing
                )
                status = reply.status
                self.statuses[str(status)] += 1
                identity, valid = self._receipt(reply)
                if status in {200, 202} and valid:
                    outcome = initial_outcome = "accepted"
                elif status in {429, 503}:
                    delay, valid_header = retry_after_seconds(reply.retry_after)
                    invalid_retry_after = not valid_header
                    self.counts["invalid_retry_after"] += invalid_retry_after
                    outcome, initial_outcome, retry = "rejected", "rejected", True
                    self.counts["rejected"] += 1
                elif status in {200, 202}:
                    outcome, initial_outcome, retry = "unknown", "unknown", True
                    if identity is not None:
                        # The POST slot was released before this query waits
                        # for all first IDs to be started or marked not_sent.
                        query = await self._request(
                            "GET", row, identity, timing=query_timing
                        )
                        query_status = query.status
                        self.query_statuses[str(query_status)] += 1
                        returned, valid = self._receipt(query)
                        if query.status == 200 and valid and returned == identity:
                            outcome, retry = "accepted", False
                        elif query.status in {429, 503}:
                            delay, valid_header = retry_after_seconds(query.retry_after)
                            invalid_retry_after = not valid_header
                            self.counts["invalid_retry_after"] += invalid_retry_after
                else:
                    outcome = initial_outcome = "permanent_rejection"
                    self.counts["permanent_rejection"] += 1
            except Exception:  # noqa: BLE001 - never log client exceptions or URLs
                if "started" not in post_timing:
                    if attempt == 1:
                        self._mark_first(row)
                    break
                outcome, initial_outcome, retry = "unknown", "unknown", True
            count = attempt
            if initial_outcome == "unknown":
                self.counts["unknown"] += 1
            if attempt == 1:
                self.first_statuses[str(status)] += 1
                self.counts[f"first_{initial_outcome}"] += 1
            event = {
                "kind": "http_attempt",
                "seq": row["seq"],
                "attempt": attempt,
                "started_ns": int(post_timing["started"] * 10**9),
                "ended_ns": int(post_timing["ended"] * 10**9),
                "status": status,
                "outcome": outcome,
                "queried": "started" in query_timing,
            }
            if invalid_retry_after:
                event["invalid_retry_after"] = True
            if "started" in query_timing:
                event["query_status"] = query_status
                event["query_started_ns"] = int(query_timing["started"] * 10**9)
                event["query_ended_ns"] = int(query_timing["ended"] * 10**9)
            self.sink.append(event)
            if outcome == "accepted":
                self.last_accepted_at = self.clock()
                self.counts["accepted"] += 1
                return {
                    "seq": row["seq"],
                    "attempts": count,
                    "receipt_id": identity,
                    "outcome": "accepted",
                }
            if not retry:
                return {
                    "seq": row["seq"],
                    "attempts": count,
                    "receipt_id": None,
                    "outcome": outcome,
                }
            if self.clock() + delay >= self.deadline:
                break
            await self.sleep(delay)
        return {
            "seq": row["seq"],
            "attempts": count,
            "receipt_id": None,
            "outcome": "deadline_or_attempts_exhausted",
        }

    async def run(self):
        outcomes = await asyncio.gather(*(self._one(row) for row in self.inputs))
        first = min(self.first_starts) if self.first_starts else None
        window = max(self.first_starts) - first if first is not None else None
        return {
            "stage": "http",
            "run_id": self.config["run_id"],
            "count": len(self.inputs),
            "counts": dict(self.counts),
            "statuses": dict(self.statuses),
            "first_statuses": dict(self.first_statuses),
            "query_statuses": dict(self.query_statuses),
            "concurrency_peak": self.peak,
            "first_request_window_seconds": window,
            "burst_window_passed": len(self.first_starts) == len(self.inputs)
            and window is not None
            and window <= 5,
            "all_responses_accepted": self.counts["accepted"] == len(self.inputs),
            "local_response_deadline_passed": self.counts["accepted"]
            == len(self.inputs)
            and self.last_accepted_at is not None
            and first is not None
            and self.last_accepted_at - first <= 60,
            "request_amplification": self.counts["post_requests"] / len(self.inputs),
            "retry_after_contract_passed": self.counts["invalid_retry_after"] == 0,
            "id_outcomes": outcomes,
            "dlr_acceptance_verified": False,
            "timing_scope": "producer-local monotonic only; platform arrival/commit measured separately",
        }


async def http_send(config):
    import httpx

    parsed = urlsplit(config["ingress_url"])
    token = config.get("token")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/api/ingress/[A-Za-z0-9_-]{1,128}", parsed.path)
        or not isinstance(token, str)
        or not 1 <= len(token) <= 4096
        or not token.isascii()
        or not token.isprintable()
    ):
        raise FixtureError("invalid_private_http_configuration")
    inputs = read_inputs(config)
    sink = PrivateLog(
        _private_path(config["output_path"]) / f"{config['run_id']}-http.private.jsonl",
        max_bytes=HTTP_LOG_MAX_BYTES,
    )
    try:
        async with httpx.AsyncClient(
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=128),
        ) as client:

            async def send(method, row, receipt_id):
                url = config["ingress_url"]
                if method == "GET":
                    url += f"/receipts/{receipt_id}"
                headers = {"Authorization": f"Bearer {token}"}
                body = None
                if method == "POST":
                    body = base64.b64decode(row["raw_base64"], validate=True)
                    headers.update(
                        {
                            "Content-Type": "application/json",
                            "Idempotency-Key": row["message_id"],
                        }
                    )
                async with client.stream(
                    method, url, headers=headers, content=body
                ) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 16384:
                            raise FixtureError("http_response_too_large")
                        raw.extend(chunk)
                    return HttpReply(
                        response.status_code,
                        bytes(raw),
                        response.headers.get("Retry-After"),
                    )

            return await HttpSender(config, inputs, sink, send).run()
    finally:
        sink.close()


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
                "x-max-length": 12000,
                "x-max-length-bytes": 64 * 1024 * 1024,
                "x-overflow": "reject-publish",
            },
            callback=self.on_queue_ready,
        )

    def on_queue_ready(self, _frame: Any) -> None:
        self.channel.confirm_delivery(
            self.on_confirmation, callback=self.on_confirm_ready
        )

    def on_confirm_ready(self, _frame: Any) -> None:
        self.connection.ioloop.call_later(
            5,
            lambda: (
                self.fail("publisher_burst_deadline") if not self.complete else None
            ),
        )
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
            15, lambda: self.fail("publisher_total_deadline")
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
        "command", choices=("generate", "prepare-target", "publish", "http", "verify")
    )
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result = {
            "generate": generate,
            "prepare-target": prepare_target,
            "publish": publish,
            "http": lambda value: asyncio.run(http_send(value)),
            "verify": verify,
        }[args.command](config)
        write_result(config, args.command, result)
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "id_outcomes"},
                default=str,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return (
            2
            if (
                result.get("target_oracle_passed") is False
                or result.get("all_responses_accepted") is False
                or result.get("retry_after_contract_passed") is False
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
