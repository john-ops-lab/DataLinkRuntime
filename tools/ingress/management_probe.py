#!/usr/bin/env python3
"""Read-only, bounded 1 Hz management sampling beside the frozen producer.

All configuration and evidence paths must be outside the repository. No response
body, URL, token, exception text, event payload or adapter name is persisted.
The producer wrapper only adds clock provenance; workload.py remains unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCHEMA = "dlr.ingress.management-probe.v1"
ENDPOINTS = ("adapters", "events", "backlog")
STATES = ("pending", "processing", "succeeded", "failed", "skipped", "superseded", "abandoned")
ALIASES = ("accepted", "replayed", "coalesced")
QUOTA_KEYS = (
    "pending_count", "pending_bytes", "history_count", "history_bytes", "dedupe_count",
    "reserved_control_count", "reserved_control_bytes", "rate_count",
)
LIMIT_KEYS = ("pending_count", "pending_bytes", "history_count", "history_bytes", "dedupe_count", "rate")
MAX_LOG_BYTES = 16 * 1024**2
MAX_RESPONSE_BYTES = 2 * 1024**2
WORKLOAD_SHA256 = "a1154f3b9cd478c38c8108fc1b15d08eb01edd2884e75a7a7144544cc38dbcaf"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
NS = 10**9


class ProbeError(Exception):
    """Only stable, non-sensitive codes may reach CLI diagnostics."""


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def private_path(value: str | Path) -> Path:
    result = Path(value).expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    if result == repository or repository in result.parents:
        raise ProbeError("private_path_inside_repository")
    return result


def bounded_json(path: Path, limit: int = 65536) -> Any:
    if path.stat().st_size > limit:
        raise ProbeError("input_file_too_large")
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as error:
        raise ProbeError("invalid_json_file") from error


class PrivateLog:
    def __init__(self, path: Path, limit: int) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        self.limit, self.size = limit, 0

    def append(self, value: Any) -> None:
        raw = encoded(value) + b"\n"
        if self.size + len(raw) > self.limit:
            raise ProbeError("probe_evidence_budget_exceeded")
        self.handle.write(raw)
        self.handle.flush()
        self.size += len(raw)

    def close(self) -> None:
        self.handle.close()


def write_private(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    log = PrivateLog(temporary, 256 * 1024)
    try:
        log.append(value)
        os.fsync(log.handle.fileno())
        # The final name becomes visible only when the complete receipt exists.
        # link is atomic and cannot replace earlier evidence.
        os.link(temporary, path)
    finally:
        log.close()
        temporary.unlink(missing_ok=True)


def paths(config: dict[str, Any]) -> dict[str, Path]:
    prefix = private_path(config["output_path"]) / config["run_id"]
    return {name: Path(f"{prefix}-{suffix}") for name, suffix in {
        "log": "management.private.jsonl", "ready": "management-ready.private.json",
        "pid": "management-pid.private.json", "stop": "management.stop",
        "producer_start": "producer-clock-start.private.json",
        "producer_end": "producer-clock-end.private.json", "analysis": "management-analysis.private.json",
    }.items()}


def _integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProbeError(code)
    return value


def load_config(path: Path) -> dict[str, Any]:
    value = bounded_json(private_path(path))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ProbeError("invalid_probe_schema")
    if not isinstance(value.get("run_id"), str) or not RUN_ID.fullmatch(value["run_id"]):
        raise ProbeError("invalid_run_id")
    _integer(value.get("subscription_id"), 1, 2**63 - 1, "invalid_subscription_id")
    _integer(value.get("count"), 10000, 10000, "formal_count_must_be_10000")
    if value.get("stage") not in {"http", "publish"}:
        raise ProbeError("invalid_producer_stage")
    base = urlsplit(value.get("api_base", ""))
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password or base.path not in {"", "/"} or base.query or base.fragment:
        raise ProbeError("invalid_private_api_base")
    token = value.get("admin_token")
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not token.isascii() or not token.isprintable():
        raise ProbeError("invalid_private_admin_token")
    sub = value["subscription_id"]
    expected = {"adapters": "/api/adapters", "events": f"/api/event-subscriptions/{sub}/events?limit=20", "backlog": f"/api/event-subscriptions/{sub}"}
    if value.get("urls") != expected:
        raise ProbeError("probe_endpoints_must_be_fixed_get_routes")
    for name in ("output_path", "producer_log_path", "producer_result_path"):
        value[name] = str(private_path(value[name]))
    defaults = {"baseline_samples": 20, "post_drain_seconds": 600, "deadline_seconds": 1800, "request_timeout_seconds": 5, "response_max_bytes": MAX_RESPONSE_BYTES, "log_max_bytes": MAX_LOG_BYTES}
    bounds = {"baseline_samples": (20, 60), "post_drain_seconds": (600, 600), "deadline_seconds": (622, 1800), "request_timeout_seconds": (1, 5), "response_max_bytes": (1024, MAX_RESPONSE_BYTES), "log_max_bytes": (1024, MAX_LOG_BYTES)}
    for name, default in defaults.items():
        value[name] = _integer(value.get(name, default), *bounds[name], f"invalid_{name}")
    return value


def config_fingerprint(config: dict[str, Any]) -> str:
    # Paths, addresses and secrets never enter evidence, even as a config copy.
    public = {name: config[name] for name in ("schema", "run_id", "subscription_id", "count", "stage", "baseline_samples", "post_drain_seconds", "deadline_seconds", "request_timeout_seconds", "response_max_bytes", "log_max_bytes")}
    return hashlib.sha256(encoded(public)).hexdigest()


def clock_domain() -> dict[str, str]:
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.readlink("/proc/self/ns/time")
        children = os.readlink("/proc/self/ns/time_for_children")
    except OSError as error:
        raise ProbeError("linux_monotonic_domain_unavailable") from error
    if not re.fullmatch(r"[0-9a-f-]{36}", boot) or namespace != children:
        raise ProbeError("producer_clock_inheritance_unproven")
    return {"clock": "CLOCK_MONOTONIC", "boot_id": boot, "time_namespace": namespace, "children_time_namespace": children}


def _numbers(value: Any, keys: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ProbeError("invalid_numeric_snapshot")
    return {key: _integer(value[key], 0, 2**63 - 1, "invalid_numeric_snapshot") for key in keys if key in value}


def safe_response(endpoint: str, body: bytes, subscription_id: int) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise ProbeError("invalid_response_json") from error
    if endpoint == "adapters":
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ProbeError("invalid_adapters_response")
        return {"item_count": len(value)}
    if endpoint == "events":
        if not isinstance(value, dict) or not isinstance(value.get("items"), list) or len(value["items"]) > 20:
            raise ProbeError("invalid_events_response")
        return {"item_count": len(value["items"]), "total": _integer(value.get("total"), len(value["items"]), 2**63 - 1, "invalid_events_total")}
    if not isinstance(value, dict) or type(value.get("id")) is not int or value["id"] != subscription_id:
        raise ProbeError("invalid_backlog_identity")
    counts = _numbers(value.get("event_counts"), STATES + ALIASES)
    if set(value["event_counts"]) - set(STATES + ALIASES) or "accepted" not in counts:
        raise ProbeError("unknown_receipt_counter")
    counts = {**dict.fromkeys(STATES, 0), **counts}
    if counts["accepted"] != sum(counts[state] for state in STATES):
        raise ProbeError("receipt_count_alias_mismatch")
    oldest = value.get("oldest_pending_at")
    oldest_epoch = None
    if oldest is not None:
        try:
            parsed = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            oldest_epoch = parsed.timestamp()
            if not math.isfinite(oldest_epoch):
                raise ValueError
        except (TypeError, ValueError, OverflowError) as error:
            raise ProbeError("invalid_oldest_pending_time") from error
    return {"counts": counts, "quota": _numbers(value.get("quota"), QUOTA_KEYS), "limits": _numbers(value.get("limits"), LIMIT_KEYS), "oldest_pending_unix_seconds": oldest_epoch}


class HttpReader:
    """One request, no redirects/retries; outer timeout includes body streaming."""

    def __init__(self, client: Any, config: dict[str, Any]) -> None:
        self.client, self.config = client, config

    async def __call__(self, endpoint: str) -> tuple[int, bytes]:
        async with self.client.stream("GET", self.config["api_base"].rstrip("/") + self.config["urls"][endpoint], headers={"Authorization": f"Bearer {self.config['admin_token']}", "Accept-Encoding": "identity"}) as reply:
            if reply.status_code != 200:
                return reply.status_code, b""
            if reply.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ProbeError("encoded_response_not_allowed")
            if int(reply.headers.get("Content-Length", "0")) > self.config["response_max_bytes"]:
                raise ProbeError("response_body_budget_exceeded")
            raw = bytearray()
            async for chunk in reply.aiter_raw(chunk_size=65536):
                if len(raw) + len(chunk) > self.config["response_max_bytes"]:
                    raise ProbeError("response_body_budget_exceeded")
                raw.extend(chunk)
            return reply.status_code, bytes(raw)


class Sampler:
    def __init__(self, config: dict[str, Any], sink: Any, reader: Any, *, clock=time.monotonic_ns, sleep=asyncio.sleep, domain=None, ready_sink=None, stop_requested=None) -> None:
        self.config, self.sink, self.reader = config, sink, reader
        self.clock, self.sleep = clock, sleep
        self.domain = domain if domain is not None else clock_domain()
        self.ready_sink = ready_sink or (lambda value: None)
        self.stop_requested = stop_requested or (lambda: False)
        self.pending: dict[str, asyncio.Task] = {}
        self.successes: Counter[str] = Counter()
        self.latest: dict[str, dict[str, Any]] = {}
        self.active = self.peak = 0
        self.errors = 0
        self.ready_ns: int | None = None
        self.done_ns: int | None = None
        self.first_zero: dict[str, Any] | None = None
        self.start_ns = self.clock()
        self.fingerprint = config_fingerprint(config)

    async def one(self, endpoint: str, slot: int, scheduled_ns: int) -> None:
        started = self.clock()
        self.active += 1
        self.peak = max(self.peak, self.active)
        status, timeout, code, safe = 0, False, None, None
        try:
            status, body = await asyncio.wait_for(self.reader(endpoint), self.config["request_timeout_seconds"])
            if status == 200:
                safe = safe_response(endpoint, body, self.config["subscription_id"])
            else:
                code = "http_status_not_200"
        except (TimeoutError, asyncio.TimeoutError):
            timeout, code = True, "request_timeout"
        except asyncio.CancelledError:
            code = "request_cancelled_at_probe_deadline"
        except Exception as error:  # noqa: BLE001 - never retain driver text
            code = str(error) if isinstance(error, ProbeError) else "request_transport_failed"
        finally:
            self.active -= 1
        ended = self.clock()
        row = {"kind": "sample", "endpoint": endpoint, "slot": slot, "scheduled_ns": scheduled_ns, "request_begin_ns": started, "response_end_ns": ended, "elapsed_ms": (ended - started) / 10**6, "status": status, "timeout": timeout, "error_code": code}
        if safe is not None:
            row["snapshot"] = safe
        self.sink.append(row)
        self.latest[endpoint] = row
        if code is not None:
            self.errors += 1
            return
        self.successes[endpoint] += 1
        if endpoint == "backlog":
            counts = safe["counts"]
            if counts["accepted"] == 0 and self.first_zero is None:
                self.first_zero = {"request_begin_ns": started, "response_end_ns": ended}
            if counts["accepted"] == self.config["count"] and counts["succeeded"] == self.config["count"] and not any(counts[state] for state in STATES if state != "succeeded") and self.done_ns is None:
                self.done_ns = ended
        if self.ready_ns is None and self.errors == 0 and all(self.successes[name] >= self.config["baseline_samples"] for name in ENDPOINTS) and self.latest["backlog"]["snapshot"]["counts"]["accepted"] == 0:
            self.ready_ns = ended
            ready = {"schema": SCHEMA, "kind": "ready", "run_id": self.config["run_id"], "config_fingerprint": self.fingerprint, "monotonic_ns": ended, "clock_domain": self.domain, "baseline_samples": dict(self.successes), "first_zero_poll": self.first_zero, "accepted": 0}
            self.sink.append(ready)
            self.ready_sink(ready)

    async def run(self) -> dict[str, Any]:
        self.sink.append({"kind": "start", "schema": SCHEMA, "run_id": self.config["run_id"], "config_fingerprint": self.fingerprint, "monotonic_ns": self.start_ns, "clock_domain": self.domain, "deadline_seconds": self.config["deadline_seconds"], "interval_seconds": 1, "max_concurrency": 3})
        deadline = self.start_ns + self.config["deadline_seconds"] * NS
        slot, reason = 0, "deadline"
        try:
            while self.clock() < deadline:
                for name, task in list(self.pending.items()):
                    if task.done():
                        task.result()
                        del self.pending[name]
                if self.stop_requested():
                    reason = "external_stop"
                    break
                goal = self.done_ns + self.config["post_drain_seconds"] * NS if self.done_ns else None
                if goal is not None and not self.pending and all(self.latest.get(name, {}).get("response_end_ns", 0) >= goal for name in ENDPOINTS):
                    reason = "post_drain_complete"
                    break
                scheduled = self.start_ns + slot * NS
                now = self.clock()
                if now < scheduled:
                    await self.sleep(min((scheduled - now) / NS, (deadline - now) / NS))
                    continue
                for name in ENDPOINTS:
                    if name in self.pending or now >= scheduled + NS:
                        self.sink.append({"kind": "missed", "endpoint": name, "slot": slot, "scheduled_ns": scheduled, "observed_ns": now, "error_code": "previous_request_still_in_flight" if name in self.pending else "sampling_start_late"})
                        self.errors += 1
                    else:
                        self.pending[name] = asyncio.create_task(self.one(name, slot, scheduled))
                slot += 1
                # Always yield, including fake-clock unit tests.
                await self.sleep(0)
        finally:
            for task in self.pending.values():
                if not task.done():
                    task.cancel()
            if self.pending:
                await asyncio.gather(*self.pending.values())
        result = {"kind": "end", "monotonic_ns": self.clock(), "reason": reason, "slots": slot, "concurrency_peak": self.peak, "errors": self.errors, "ready": self.ready_ns is not None, "done_observed": self.done_ns is not None}
        self.sink.append(result)
        return result


def percentile95(values: list[float]) -> float | None:
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None


def _read_rows(path: Path, limit: int, max_rows: int) -> list[dict[str, Any]]:
    if path.stat().st_size > limit:
        raise ProbeError("evidence_file_too_large")
    rows = []
    with path.open("rb") as source:
        for line in source:
            if len(rows) >= max_rows or len(line) > 16384:
                raise ProbeError("evidence_row_budget_exceeded")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ProbeError("invalid_evidence_row")
            rows.append(value)
    return rows


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for raw in iter(lambda: source.read(65536), b""):
            digest.update(raw)
    return digest.hexdigest()


def producer_evidence(config: dict[str, Any], ready: dict[str, Any], domain: dict[str, Any]) -> dict[str, Any]:
    names = paths(config)
    start = bounded_json(names["producer_start"])
    end = bounded_json(names["producer_end"])
    log = private_path(config["producer_log_path"])
    result_path = private_path(config["producer_result_path"])
    for receipt in (start, end):
        if receipt.get("run_id") != config["run_id"] or receipt.get("stage") != config["stage"] or receipt.get("clock_domain") != domain or receipt.get("workload_sha256") != WORKLOAD_SHA256 or receipt.get("config_fingerprint") != config_fingerprint(config):
            raise ProbeError("producer_clock_provenance_mismatch")
    if end.get("returncode") != 0 or end.get("log_sha256") != file_digest(log) or end.get("result_sha256") != file_digest(result_path):
        raise ProbeError("producer_did_not_finish_unchanged")
    if start.get("monotonic_ns", 0) < ready["monotonic_ns"] or end.get("monotonic_ns", 0) <= start["monotonic_ns"]:
        raise ProbeError("producer_not_started_after_ready")
    if log.stat().st_size > 64 * 1024**2:
        raise ProbeError("producer_log_too_large")
    first = None
    seen: set[int | str] = set()
    with log.open("rb") as source:
        for index, line in enumerate(source):
            if index >= 200001 or len(line) > 16384:
                raise ProbeError("producer_log_row_budget_exceeded")
            row = json.loads(line)
            if config["stage"] == "http" and row.get("kind") == "http_attempt" and row.get("attempt") == 1:
                tick, identity = row.get("started_ns"), row.get("seq")
            elif config["stage"] == "publish" and row.get("kind") == "publish_started":
                tick, identity = row.get("monotonic_ns"), row.get("delivery_tag")
            else:
                continue
            if type(identity) is not int or not 1 <= identity <= config["count"] or identity in seen or type(tick) is not int or not start["monotonic_ns"] <= tick <= end["monotonic_ns"]:
                raise ProbeError("producer_first_request_evidence_invalid")
            seen.add(identity)
            first = tick if first is None else min(first, tick)
    value = bounded_json(result_path, 4 * 1024**2)
    if value.get("run_id") != config["run_id"] or value.get("count") != config["count"] or value.get("stage") != config["stage"] or len(seen) != config["count"] or first is None or value.get("burst_window_passed") is not True:
        raise ProbeError("producer_first_round_incomplete")
    if config["stage"] == "http":
        if value.get("all_responses_accepted") is not True or value.get("retry_after_contract_passed") is not True or value.get("local_response_deadline_passed") is not True or value.get("concurrency_peak", 129) > 125:
            raise ProbeError("http_producer_contract_failed")
    elif value.get("all_broker_confirmed") is not True or value.get("broker_acked") != config["count"] or value.get("failure_code") is not None:
        raise ProbeError("rabbitmq_producer_contract_failed")
    return {"sender_begin_ns": first, "clock_domain_proven": True, "first_request_count": len(seen)}


def evaluate(config: dict[str, Any], rows: list[dict[str, Any]], producer: dict[str, Any] | None, producer_error: str | None = None) -> dict[str, Any]:
    failures: set[str] = set()
    starts = [row for row in rows if row.get("kind") == "start"]
    ends = [row for row in rows if row.get("kind") == "end"]
    ready_rows = [row for row in rows if row.get("kind") == "ready"]
    samples = [row for row in rows if row.get("kind") == "sample"]
    if len(starts) != 1 or len(ends) != 1 or len(ready_rows) != 1:
        failures.add("probe_lifecycle_incomplete")
    ready = ready_rows[0] if len(ready_rows) == 1 else None
    if not producer or producer.get("clock_domain_proven") is not True or producer_error:
        failures.add(producer_error or "producer_clock_evidence_missing")
        producer = None
    if any(row.get("kind") == "missed" for row in rows):
        failures.add("sampling_slots_missed")
    if any(row.get("status") != 200 or row.get("timeout") or row.get("error_code") is not None for row in samples):
        failures.add("management_request_failed")
    slots: Counter[tuple[str, int]] = Counter((row.get("endpoint"), row.get("slot")) for row in samples)
    if ends:
        expected = {(endpoint, slot) for endpoint in ENDPOINTS for slot in range(ends[0].get("slots", 0))}
        if set(slots) != expected or any(value != 1 for value in slots.values()):
            failures.add("sampling_coverage_incomplete")
        if ends[0].get("reason") != "post_drain_complete" or ends[0].get("concurrency_peak", 4) > 3:
            failures.add("probe_did_not_finish_post_drain")
    for row in samples:
        begin, end = row.get("request_begin_ns", 0), row.get("response_end_ns", -1)
        if begin < row.get("scheduled_ns", 0) or begin >= row.get("scheduled_ns", 0) + NS or end < begin or end - begin > config["request_timeout_seconds"] * NS or abs(row.get("elapsed_ms", -1) - (end - begin) / 10**6) > 0.001:
            failures.add("sample_time_budget_invalid")
    backlogs = sorted((row for row in samples if row.get("endpoint") == "backlog" and row.get("snapshot") is not None and row.get("error_code") is None), key=lambda row: row["response_end_ns"])
    accepted = next((row for row in backlogs if row["snapshot"]["counts"]["accepted"] == config["count"]), None)
    done = next((row for row in backlogs if row["snapshot"]["counts"]["accepted"] == config["count"] and row["snapshot"]["counts"]["succeeded"] == config["count"] and all(row["snapshot"]["counts"][state] == 0 for state in STATES if state != "succeeded")), None)
    previous = next((row for row in reversed(backlogs) if accepted and row["response_end_ns"] < accepted["response_end_ns"] and row["snapshot"]["counts"]["accepted"] < config["count"]), None)
    first_zero = next((row for row in backlogs if row["snapshot"]["counts"]["accepted"] == 0), None)
    if not accepted or not done or not previous or not first_zero or not ready or first_zero["response_end_ns"] > ready["monotonic_ns"]:
        failures.add("accept_or_completion_interval_unknown")
    if any(row["snapshot"]["counts"]["accepted"] > config["count"] for row in backlogs):
        failures.add("subscription_not_fresh_or_count_exceeded")
    if any(right["snapshot"]["counts"]["accepted"] < left["snapshot"]["counts"]["accepted"] for left, right in pairwise(backlogs)):
        failures.add("accepted_count_regressed")
    if done and any(row["response_end_ns"] > done["response_end_ns"] and (row["snapshot"]["counts"]["succeeded"] != config["count"] or any(row["snapshot"]["counts"][state] for state in STATES if state != "succeeded")) for row in backlogs):
        failures.add("post_drain_business_state_regressed")
    timing: dict[str, Any] = {"scope": "causal conservative bounds on the producer/probe Linux CLOCK_MONOTONIC domain; no cross-VM UTC subtraction", "known": False}
    if accepted and previous and done and producer and ready:
        sender = producer["sender_begin_ns"]
        if sender < ready["monotonic_ns"] or accepted["response_end_ns"] < sender or done["response_end_ns"] < sender:
            failures.add("sender_time_order_invalid")
        else:
            acceptance_upper = (accepted["response_end_ns"] - sender) / NS
            drain_upper = (done["response_end_ns"] - previous["request_begin_ns"]) / NS
            timing.update(known=True, sender_begin_ns=sender, actual_accept_interval_ns={"lower": previous["request_begin_ns"], "upper": accepted["response_end_ns"]}, actual_done_upper_ns=done["response_end_ns"], acceptance_seconds_upper=acceptance_upper, completion_after_accept_seconds_upper=drain_upper, acceptance_limit_seconds=60, completion_limit_seconds=600)
            if acceptance_upper > 60 or drain_upper > 600:
                failures.add("causal_time_upper_bound_exceeded")
    metrics: dict[str, Any] = {}
    for endpoint in ENDPOINTS:
        own = [row for row in samples if row.get("endpoint") == endpoint and row.get("status") == 200 and row.get("error_code") is None]
        baseline = [row["elapsed_ms"] for row in own if ready and row["response_end_ns"] <= ready["monotonic_ns"]]
        load = [row["elapsed_ms"] for row in own if producer and done and row["request_begin_ns"] >= producer["sender_begin_ns"] and row["request_begin_ns"] <= done["response_end_ns"]]
        post = [row["elapsed_ms"] for row in own if done and row["request_begin_ns"] > done["response_end_ns"]]
        idle95 = percentile95(baseline)
        limit = max(500.0, idle95 * 2) if idle95 is not None else None
        groups = {"baseline": baseline, "load_and_drain": load, "post_drain": post}
        metrics[endpoint] = {"limit_ms": limit, **{name: {"samples": len(values), "p95_ms": percentile95(values)} for name, values in groups.items()}}
        if len(baseline) < config["baseline_samples"] or not load or not post:
            failures.add("endpoint_phase_samples_missing")
        if limit is not None and any(values and percentile95(values) > limit for values in (load, post)):
            failures.add("management_p95_exceeded")
        if done and not any(row["response_end_ns"] >= done["response_end_ns"] + config["post_drain_seconds"] * NS for row in own):
            failures.add("post_drain_600s_not_observed")
    return {"schema": SCHEMA, "run_id": config["run_id"], "management_and_timing_passed": not failures, "failure_codes": sorted(failures), "timing": timing, "latency": metrics, "sample_count": len(samples), "first_zero_poll": {key: first_zero[key] for key in ("request_begin_ns", "response_end_ns")} if first_zero else None, "independent_business_oracle_required": True, "root_resource_verification_required": True, "capacity_acceptance_declared": False}


def analyze(config: dict[str, Any]) -> dict[str, Any]:
    rows = _read_rows(paths(config)["log"], config["log_max_bytes"], 12000)
    producer, error = None, None
    try:
        ready = next(row for row in rows if row.get("kind") == "ready")
        start = next(row for row in rows if row.get("kind") == "start")
        if start.get("config_fingerprint") != config_fingerprint(config) or ready.get("config_fingerprint") != config_fingerprint(config) or start.get("clock_domain") != ready.get("clock_domain"):
            raise ProbeError("probe_clock_provenance_mismatch")
        producer = producer_evidence(config, ready, start["clock_domain"])
    except Exception as failure:  # noqa: BLE001
        error = str(failure) if isinstance(failure, ProbeError) else "producer_clock_evidence_missing"
    return evaluate(config, rows, producer, error)


async def start_probe(config: dict[str, Any]) -> dict[str, Any]:
    import httpx

    names = paths(config)
    if names["stop"].exists():
        raise ProbeError("stale_stop_file")
    domain = clock_domain()
    write_private(names["pid"], {"pid": os.getpid(), "clock_domain": domain, "run_id": config["run_id"]})
    sink = PrivateLog(names["log"], config["log_max_bytes"])
    try:
        async with httpx.AsyncClient(timeout=config["request_timeout_seconds"], follow_redirects=False, trust_env=False, transport=httpx.AsyncHTTPTransport(retries=0, limits=httpx.Limits(max_connections=3, max_keepalive_connections=3))) as client:
            sampler = Sampler(config, sink, HttpReader(client, config), domain=domain, ready_sink=lambda value: write_private(names["ready"], value), stop_requested=names["stop"].exists)
            await sampler.run()
    finally:
        sink.close()
    result = analyze(config)
    write_private(names["analysis"], result)
    return result


def wait_ready(config: dict[str, Any], timeout: int) -> dict[str, Any]:
    _integer(timeout, 1, 120, "invalid_wait_timeout")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if paths(config)["ready"].exists():
            value = bounded_json(paths(config)["ready"])
            if value.get("config_fingerprint") != config_fingerprint(config) or value.get("clock_domain") != clock_domain() or value.get("accepted") != 0:
                raise ProbeError("ready_receipt_mismatch")
            pid = bounded_json(paths(config)["pid"])
            try:
                os.kill(_integer(pid.get("pid"), 1, 2**31 - 1, "invalid_probe_pid"), 0)
            except OSError as error:
                raise ProbeError("probe_not_running") from error
            return {"ready": True, "run_id": config["run_id"], "monotonic_ns": value["monotonic_ns"]}
        time.sleep(0.1)
    raise ProbeError("ready_deadline_exceeded")


def run_producer(config: dict[str, Any], workload_config_path: Path, stage: str) -> dict[str, Any]:
    workload_path = Path(__file__).with_name("workload.py")
    if file_digest(workload_path) != WORKLOAD_SHA256:
        raise ProbeError("frozen_workload_changed")
    ready = bounded_json(paths(config)["ready"])
    domain = clock_domain()
    if ready.get("clock_domain") != domain or ready.get("config_fingerprint") != config_fingerprint(config):
        raise ProbeError("ready_receipt_mismatch")
    workload_config_path = private_path(workload_config_path)
    producer = bounded_json(workload_config_path)
    if stage != config["stage"] or producer.get("run_id") != config["run_id"] or producer.get("kind") != "capacity" or producer.get("count") != config["count"]:
        raise ProbeError("producer_config_identity_mismatch")
    if stage == "http" and (type(producer.get("http_concurrency")) is not int or not 1 <= producer["http_concurrency"] <= 125):
        raise ProbeError("producer_plus_management_exceeds_128")
    producer_output = private_path(producer["output_path"])
    if private_path(config["producer_log_path"]) != producer_output / f"{config['run_id']}-{stage}.private.jsonl" or private_path(config["producer_result_path"]) != producer_output / f"{config['run_id']}-{stage}.private.json":
        raise ProbeError("producer_evidence_paths_mismatch")
    witness = {"schema": SCHEMA, "run_id": config["run_id"], "stage": stage, "clock_domain": domain, "workload_sha256": WORKLOAD_SHA256, "config_fingerprint": config_fingerprint(config)}
    write_private(paths(config)["producer_start"], {**witness, "monotonic_ns": time.monotonic_ns()})
    try:
        completed = subprocess.run([sys.executable, str(workload_path), stage, "--config", str(workload_config_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        returncode = -1
    result = {**witness, "monotonic_ns": time.monotonic_ns(), "returncode": returncode, "log_sha256": file_digest(private_path(config["producer_log_path"])) if Path(config["producer_log_path"]).exists() else None, "result_sha256": file_digest(private_path(config["producer_result_path"])) if Path(config["producer_result_path"]).exists() else None}
    write_private(paths(config)["producer_end"], result)
    return {"run_id": config["run_id"], "stage": stage, "returncode": returncode, "clock_domain_proven": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "wait-ready", "run-producer", "analyze"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workload-config", type=Path)
    parser.add_argument("--stage", choices=("http", "publish"))
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.command == "start":
            value = asyncio.run(start_probe(config))
        elif args.command == "wait-ready":
            value = wait_ready(config, args.timeout)
        elif args.command == "run-producer":
            if args.workload_config is None or args.stage is None:
                raise ProbeError("producer_arguments_required")
            value = run_producer(config, args.workload_config, args.stage)
        else:
            value = analyze(config)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0 if value.get("management_and_timing_passed", value.get("returncode", 0) == 0) else 2
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"failed": True, "code": str(error) if isinstance(error, ProbeError) else "management_probe_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
