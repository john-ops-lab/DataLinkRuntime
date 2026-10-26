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
import heapq
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
HTTP_CONCURRENCY = 128
HTTP_FOLLOWUP_STARTS_PER_SECOND = 500
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


@dataclass(slots=True)
class _HttpItem:
    index: int
    row: dict[str, Any]
    attempts: int = 0
    receipt_id: int | None = None
    event: dict[str, Any] | None = None
    delay: int = 1


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
        concurrency = config.get("http_concurrency", HTTP_CONCURRENCY)
        if type(concurrency) is not int or not 1 <= concurrency <= HTTP_CONCURRENCY:
            raise FixtureError("invalid_http_concurrency")
        self.concurrency = concurrency
        self.slots = asyncio.Semaphore(concurrency)
        self.followup_start_lock = asyncio.Lock()
        self.followup_next_start = None
        self.route = urlsplit(config["ingress_url"]).path
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
        self.request_tasks_peak = self.deferred_items_peak = 0

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

    async def _acquire_start(self, *, first, deadline):
        # Only follow-ups are paced. One waiting lock owner, no stored tokens,
        # and no request slot held during the wait. Always rebase on the real
        # start so an idle period or slow event loop cannot cause catch-up.
        paced = not first
        if paced:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self.followup_start_lock.acquire(), remaining)
        try:
            if paced and self.followup_next_start is not None:
                while self.clock() < self.followup_next_start:
                    delay = self.followup_next_start - self.clock()
                    if delay >= deadline - self.clock():
                        raise TimeoutError
                    await self.sleep(delay)
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(self.slots.acquire(), remaining)
            started = self.clock()
            if started >= deadline:
                self.slots.release()
                raise TimeoutError
            if paced:
                self.followup_next_start = started + 1 / HTTP_FOLLOWUP_STARTS_PER_SECOND
            return started
        finally:
            if paced:
                self.followup_start_lock.release()

    async def _request(self, method, row, receipt_id=None, *, first=False, timing=None):
        # Waiting for the first round or backoff never consumes an in-flight
        # HTTP slot. Queries and retries cannot overtake any first POST.
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError
        if not first:
            await asyncio.wait_for(self.first_round_done.wait(), timeout=remaining)
        deadline = min(self.deadline, self.first_deadline) if first else self.deadline
        started = await self._acquire_start(first=first, deadline=deadline)
        try:
            if first:
                self._mark_first(row, started)
            if timing is not None:
                timing["started"] = started
            self.counts["post_requests" if method == "POST" else "query_requests"] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                # Invoke transport in this task immediately after admission;
                # a separately scheduled Task could compress actual starts
                # after an event-loop stall despite spaced admission times.
                async with asyncio.timeout(min(5.0, self.deadline - started)):
                    return await self.send(method, row, receipt_id)
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
        ) == f"{self.route}/receipts/{identity}"

    def _outcome(self, item, outcome="deadline_or_attempts_exhausted"):
        return {
            "seq": item.row["seq"],
            "attempts": item.attempts,
            "receipt_id": item.receipt_id if outcome == "accepted" else None,
            "outcome": outcome,
        }

    def _emit(self, item):
        if item.event is not None:
            self.sink.append(item.event)
            item.event = None

    def _accepted(self, item):
        self._emit(item)
        self.last_accepted_at = self.clock()
        self.counts["accepted"] += 1
        return "done", 0, self._outcome(item, "accepted")

    def _retry(self, item):
        self._emit(item)
        due = self.clock() + item.delay
        if item.attempts >= 20 or due >= self.deadline:
            return "done", 0, self._outcome(item)
        return "post", due, None

    async def _post(self, item):
        attempt = item.attempts + 1
        timing = {}
        status, identity, valid = 0, None, False
        retry, initial_outcome, item.delay = True, "unknown", 1
        invalid_retry_after = False
        try:
            reply = await self._request(
                "POST", item.row, first=attempt == 1, timing=timing
            )
            status = reply.status
            self.statuses[str(status)] += 1
            identity, valid = self._receipt(reply)
            if status in {200, 202} and valid:
                initial_outcome = "accepted"
            elif status in {429, 503}:
                item.delay, valid_header = retry_after_seconds(reply.retry_after)
                invalid_retry_after = not valid_header
                self.counts["invalid_retry_after"] += invalid_retry_after
                initial_outcome = "rejected"
                self.counts["rejected"] += 1
            elif status not in {200, 202}:
                initial_outcome, retry = "permanent_rejection", False
                self.counts["permanent_rejection"] += 1
        except Exception:  # noqa: BLE001 - never log client exceptions or URLs
            if "started" not in timing:
                if attempt == 1:
                    self._mark_first(item.row)
                return "done", 0, self._outcome(item)
        item.attempts, item.receipt_id = attempt, identity
        if initial_outcome == "unknown":
            self.counts["unknown"] += 1
        if attempt == 1:
            self.first_statuses[str(status)] += 1
            self.counts[f"first_{initial_outcome}"] += 1
        item.event = {
            "kind": "http_attempt",
            "seq": item.row["seq"],
            "attempt": attempt,
            "started_ns": int(timing["started"] * 10**9),
            "ended_ns": int(timing["ended"] * 10**9),
            "status": status,
            "outcome": initial_outcome,
            "queried": False,
        }
        if invalid_retry_after:
            item.event["invalid_retry_after"] = True
        if initial_outcome == "accepted":
            return self._accepted(item)
        if initial_outcome == "unknown" and identity is not None:
            # Store one small state record instead of parking a per-ID Task
            # at the first-round barrier. The request slot is already free.
            return "query", self.clock(), None
        if not retry:
            self._emit(item)
            return "done", 0, self._outcome(item, initial_outcome)
        return self._retry(item)

    async def _query(self, item):
        timing = {}
        status, accepted = 0, False
        try:
            reply = await self._request("GET", item.row, item.receipt_id, timing=timing)
            status = reply.status
            self.query_statuses[str(status)] += 1
            returned, valid = self._receipt(reply)
            accepted = status == 200 and valid and returned == item.receipt_id
            if status in {429, 503}:
                item.delay, valid_header = retry_after_seconds(reply.retry_after)
                self.counts["invalid_retry_after"] += not valid_header
                if not valid_header:
                    item.event["invalid_retry_after"] = True
        except Exception:  # noqa: BLE001, S110 - unknown queries never invent acceptance
            pass
        if "started" in timing:
            item.event.update(
                queried=True,
                query_status=status,
                query_started_ns=int(timing["started"] * 10**9),
                query_ended_ns=int(timing["ended"] * 10**9),
            )
        if accepted:
            item.event["outcome"] = "accepted"
            return self._accepted(item)
        return self._retry(item)

    async def run(self):
        # There is one row state per finite manifest entry and C persistent
        # workers. Backoff/query state has no Task, timer or HTTP slot.
        items = [_HttpItem(index, row) for index, row in enumerate(self.inputs)]
        outcomes = [None] * len(items)
        cursor = 0
        active, deferred = {}, []
        ready = asyncio.Queue(maxsize=self.concurrency)
        completed = asyncio.Queue(maxsize=self.concurrency)

        async def worker():
            while True:
                stage, item = await ready.get()
                try:
                    result = await (
                        self._post(item) if stage == "post" else self._query(item)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - relay unchanged after bounded cleanup
                    completed.put_nowait((item, None, error))
                    return
                else:
                    completed.put_nowait((item, result, None))

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(self.concurrency, len(items)))
        ]
        try:
            while cursor < len(items) or active or deferred:
                now = self.clock()
                if now >= self.first_deadline and not self.first_round_done.is_set():
                    for item in items[cursor:]:
                        self._mark_first(item.row)
                        outcomes[item.index] = self._outcome(item)
                    cursor = len(items)
                    # A Task created before the cutoff may not have run yet.
                    # _request still checks the actual start and cannot send
                    # it late; do not strand the global first-round barrier.
                    for item in active.values():
                        self._mark_first(item.row)
                if now >= self.deadline:
                    while deferred:
                        _, _, _, item = heapq.heappop(deferred)
                        self._emit(item)
                        outcomes[item.index] = self._outcome(item)
                while len(active) < self.concurrency and now < self.deadline:
                    if cursor < len(items):
                        item, cursor = items[cursor], cursor + 1
                        stage = "post"
                    elif (
                        self.first_round_done.is_set()
                        and deferred
                        and deferred[0][0] <= now
                    ):
                        _, _, stage, item = heapq.heappop(deferred)
                    else:
                        break
                    ready.put_nowait((stage, item))
                    active[item.index] = item
                    self.request_tasks_peak = max(self.request_tasks_peak, len(active))
                    now = self.clock()
                if not active:
                    if deferred:
                        await self.sleep(
                            max(0, min(deferred[0][0], self.deadline) - self.clock())
                        )
                    continue
                wake = []
                if not self.first_round_done.is_set() and now < self.first_deadline:
                    wake.append(self.first_deadline)
                if (
                    self.first_round_done.is_set()
                    and deferred
                    and len(active) < self.concurrency
                ):
                    wake.append(deferred[0][0])
                timeout = max(0, min(wake) - now) if wake else None
                try:
                    result = (
                        await completed.get()
                        if timeout is None
                        else await asyncio.wait_for(completed.get(), timeout)
                    )
                except TimeoutError:
                    continue
                done = [result]
                while not completed.empty():
                    done.append(completed.get_nowait())
                for item, result, error in done:
                    active.pop(item.index)
                    if error is not None:
                        raise error
                    stage, due, outcome = result
                    if stage == "done":
                        outcomes[item.index] = outcome
                    else:
                        heapq.heappush(deferred, (due, item.index, stage, item))
                        self.deferred_items_peak = max(
                            self.deferred_items_peak, len(deferred)
                        )
        finally:
            for task in workers:
                task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
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
            "scheduler": {
                "request_tasks_peak": self.request_tasks_peak,
                "deferred_items_peak": self.deferred_items_peak,
                "followup_start_limit_per_second": HTTP_FOLLOWUP_STARTS_PER_SECOND,
            },
            "id_outcomes": outcomes,
            "dlr_acceptance_verified": False,
            "timing_scope": "producer-local monotonic only; platform arrival/commit measured separately",
        }


def _http_configuration(config):
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
    concurrency = config.get("http_concurrency", HTTP_CONCURRENCY)
    if type(concurrency) is not int or not 1 <= concurrency <= HTTP_CONCURRENCY:
        raise FixtureError("invalid_http_concurrency")
    return config["ingress_url"], token, concurrency


class HttpTransport:
    """Pinned pooled HTTP/1.1 client; also used by isolated loopback diagnostics."""

    def __init__(self, config):
        self.url, self.token, self.concurrency = _http_configuration(config)
        self.client = None

    async def __aenter__(self):
        import aiohttp

        if aiohttp.__version__ != "3.14.5":
            raise FixtureError("pinned_http_client_required")
        connector = aiohttp.TCPConnector(
            limit=self.concurrency,
            limit_per_host=self.concurrency,
            keepalive_timeout=5,
            ttl_dns_cache=30,
            timeout_ceil_threshold=60,
        )
        try:
            self.client = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(
                    total=5, connect=5, sock_connect=5, sock_read=5, ceil_threshold=60
                ),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
                auto_decompress=False,
                read_bufsize=16384,
                max_line_size=8190,
                max_field_size=8190,
                max_headers=128,
            )
        except BaseException:
            await connector.close()
            raise
        return self.send

    async def __aexit__(self, *_args):
        if self.client is not None:
            await self.client.close()

    async def send(self, method, row, receipt_id):
        url = self.url
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept-Encoding": "identity",
        }
        body, middlewares = None, ()
        if method == "GET":
            if type(receipt_id) is not int or receipt_id < 1:
                raise FixtureError("known_http_receipt_required")
            url += f"/receipts/{receipt_id}"
            sent = False

            async def once(request, handler):
                # aiohttp retries stale keepalive GETs by default. Its public
                # per-request middleware runs for each attempt: a second
                # dispatch stays unknown, without an uncounted wire request.
                nonlocal sent
                if sent:
                    raise FixtureError("http_query_connection_lost")
                sent = True
                return await handler(request)

            middlewares = (once,)
        elif method == "POST":
            body = base64.b64decode(row["raw_base64"], validate=True)
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Idempotency-Key": row["message_id"],
                }
            )
        else:
            raise FixtureError("unsupported_http_method")
        async with self.client.request(
            method,
            url,
            headers=headers,
            data=body,
            allow_redirects=False,
            middlewares=middlewares,
        ) as response:
            if response.content_length is not None and response.content_length > 16384:
                raise FixtureError("http_response_too_large")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                if len(raw) + len(chunk) > 16384:
                    raise FixtureError("http_response_too_large")
                raw.extend(chunk)
            return HttpReply(
                response.status, bytes(raw), response.headers.get("Retry-After")
            )


async def http_send(config):
    _http_configuration(config)
    inputs = read_inputs(config)
    sink = PrivateLog(
        _private_path(config["output_path"]) / f"{config['run_id']}-http.private.jsonl",
        max_bytes=HTTP_LOG_MAX_BYTES,
    )
    try:
        async with HttpTransport(config) as send:
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
