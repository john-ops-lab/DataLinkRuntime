#!/usr/bin/env python3
"""Measure a real, separately prepared subscription fixture using private JSON.

This driver never creates/edits subscriptions, changes limits, injects faults,
restarts containers, or declares acceptance from an accepted-message count.
Prepare task-owned fixtures separately, then use baseline (60 s), warmup (60 s),
steady (1800 s), burst (finite 10/s), observe (600 s), and oracle. Each phase
writes a private manifest, HTTP samples, database/resource samples and summary.
Baseline requires no active listener; steady requires one four-partition fixture.

Configuration keys (all actual values stay in an operator-owned private file):
  output_dir, prefix, control_url, control_token, target_database_url,
  control_sql_command: argv accepting a SQL statement as its last argument,
  producer: KafkaProducer keyword arguments,
  scenarios: [{name, subscription_id, adapter_id, topic, partitions: 4}],
  resource_command: optional argv returning numeric resource measurements as JSON.

The SQL command normally invokes psql with -Atc. It runs only SELECT statements
generated below. Resource commands are explicit operator-supplied inspection
commands; their output is restricted to numeric measurements. No shell is used.
The target is the independent table created by target_probe.py. Use a unique
prefix for every experiment; do not point this tool at unrelated business data.
Dependencies: the backend environment, including kafka-python and psycopg.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psycopg
from kafka import KafkaProducer

PHASE_SECONDS = {"baseline": 60, "warmup": 60, "steady": 1800, "observe": 600}
RELATIONS = (
    "event_receipts",
    "event_deliveries",
    "delivery_dispositions",
    "source_checkpoints",
    "subscription_quotas",
    "executions",
    "execution_attempts",
    "execution_outbox",
)


def utc() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: list[float], percent: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * percent) - 1)] if values else None


def payload(prefix: str, sequence: int, size: int) -> dict[str, Any]:
    item = {"business_id": f"{prefix}-{sequence:06d}", "sequence": sequence, "pad": ""}
    remaining = size - len(json.dumps(item, separators=(",", ":")).encode())
    if remaining < 0:
        raise ValueError("payload_size_too_small")
    # Avoid an unrealistically compressible repeated character in storage estimates.
    item["pad"] = "".join(
        hashlib.sha256(f"{prefix}:{sequence}:{block}".encode()).hexdigest()
        for block in range(math.ceil(remaining / 64))
    )[:remaining]
    return item


def encoded(item: dict[str, Any]) -> bytes:
    return json.dumps(item, separators=(",", ":"), allow_nan=False).encode()


class Probe:
    def __init__(self, config: dict[str, Any], phase: str) -> None:
        self.config = config
        self.phase = phase
        self.prefix = str(config["prefix"])
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,99}", self.prefix) is None:
            raise ValueError("invalid_task_prefix")
        self.scenarios = config["scenarios"]
        if not 1 <= len(self.scenarios) <= 16:
            raise ValueError("invalid_scenario_count")
        for case in self.scenarios:
            for key in ("subscription_id", "adapter_id", "partitions"):
                if isinstance(case[key], bool) or not isinstance(case[key], int) or case[key] <= 0:
                    raise ValueError("invalid_fixture_identity")
        self.directory = Path(config["output_dir"]).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stem = f"{self.prefix}-{phase}"
        if next(self.directory.glob(f"{self.stem}-*.private.*"), None) is not None:
            raise ValueError("phase_evidence_already_exists")
        self.events: list[dict[str, Any]] = []
        self.http: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.stop = threading.Event()
        self.start = time.monotonic()
        self.collector_error: str | None = None

    def write(self, suffix: str, data: Any) -> None:
        path = self.directory / f"{self.stem}-{suffix}.private.json"
        path.write_text(json.dumps(data, indent=2, allow_nan=False, default=str) + "\n")
        path.chmod(0o600)

    def append(self, kind: str, data: dict[str, Any]) -> None:
        path = self.directory / f"{self.stem}-{kind}.private.jsonl"
        with path.open("a") as handle:
            handle.write(json.dumps(data, allow_nan=False, default=str) + "\n")
        path.chmod(0o600)

    def sql(self, query: str) -> Any:
        result = subprocess.run(
            [*self.config["control_sql_command"], query],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("control_sql_failed")
        return json.loads(result.stdout)

    def api_sample(self) -> None:
        routes = ["/api/health", "/api/event-subscriptions/runtime-status"]
        routes.extend(f"/api/event-subscriptions/{x['subscription_id']}" for x in self.scenarios)
        for route in routes:
            start = time.monotonic()
            body: dict[str, Any] = {}
            try:
                request = Request(
                    self.config["control_url"].rstrip("/") + route,
                    headers={"Authorization": "Bearer " + self.config["control_token"]},
                )
                with urlopen(request, timeout=10) as response:
                    status = response.status
                    body = json.load(response)
            except HTTPError as error:
                status = error.code
                error.close()
            except (OSError, ValueError):
                status = 0
            row = {
                "at": utc(),
                "elapsed": time.monotonic() - self.start,
                "route": re.sub(r"/\d+", "/:id", route),
                "status": status,
                "seconds": time.monotonic() - start,
            }
            if route == "/api/health":
                # Only the bounded diagnostics contract, never arbitrary API contents.
                row["source_runtime"] = body.get("source_runtime")
                row["attempt_metrics"] = body.get("reliable_runtime", {}).get("attempt")
            elif route.endswith("/runtime-status"):
                row["runtime_status"] = {
                    key: body.get(key)
                    for key in (
                        "active_count",
                        "application_buffer_used_bytes",
                        "application_buffer_peak_bytes",
                        "application_buffer_limit_bytes",
                    )
                }
                if self.phase == "baseline" and body.get("active_count", 0):
                    raise RuntimeError("baseline_requires_no_native_owner")
            else:
                for key in (
                    "connection_state",
                    "delivery_state",
                    "last_error_code",
                    "event_counts",
                ):
                    row[key] = body.get(key)
            self.http.append(row)
            self.append("http", row)

    def snapshot(self) -> dict[str, Any]:
        subs = ",".join(str(x["subscription_id"]) for x in self.scenarios)
        adapters = ",".join(str(x["adapter_id"]) for x in self.scenarios)
        relations = ",".join("'" + name + "'" for name in RELATIONS)
        query = f"""SELECT json_build_object(
          'database_at',clock_timestamp(), 'database_bytes',pg_database_size(current_database()),
          'wal_bytes',(SELECT wal_bytes FROM pg_stat_wal),
          'relations',(SELECT json_object_agg(name,pg_total_relation_size(name::regclass))
             FROM unnest(ARRAY[{relations}]) name),
          'receipts',(SELECT coalesce(json_agg(x),'[]') FROM
             (SELECT subscription_id,state,count(*) AS count,sum(payload_bytes) AS envelope_bytes
              FROM event_receipts WHERE subscription_id IN ({subs})
              GROUP BY subscription_id,state) x),
          'executions',(SELECT coalesce(json_agg(x),'[]') FROM
             (SELECT adapter_id,status,count(*) AS count FROM executions
              WHERE adapter_id IN ({adapters})
              GROUP BY adapter_id,status) x),
          'outbox',(SELECT coalesce(json_agg(x),'[]') FROM
             (SELECT o.status,count(*) AS count FROM execution_outbox o
              JOIN executions e ON e.id=o.execution_id
              WHERE e.adapter_id IN ({adapters}) GROUP BY o.status) x),
          'quotas',(SELECT coalesce(json_agg(x),'[]') FROM
             (SELECT scope,pending_count,pending_bytes,history_count,history_bytes,
                     dedupe_count,blocked_reason
              FROM subscription_quotas WHERE scope IN ('global',
                {",".join("'sub:" + str(x["subscription_id"]) + "'" for x in self.scenarios)})) x),
          'retry',(SELECT coalesce(json_agg(x),'[]') FROM
             (SELECT e.id,e.status,e.next_attempt_at,e.attempt_count,
                (SELECT min(o.created_at) FROM execution_outbox o WHERE o.execution_id=e.id
                  AND o.dispatch_generation>1) AS retry_outbox_created_at
              FROM executions e WHERE e.adapter_id IN ({adapters}) AND
                (e.status='retry_wait' OR e.attempt_count>1)) x),
          'active_listeners',(SELECT count(*) FROM event_subscriptions
                              WHERE desired_state='enabled'))"""
        facts = self.sql(query)
        if self.phase == "baseline" and facts["active_listeners"]:
            raise RuntimeError("baseline_requires_no_listener")
        with psycopg.connect(self.config["target_database_url"], connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            target = connection.execute(
                "SELECT count(*),coalesce(sum(writes),0),"
                "pg_total_relation_size('dlr_subscription_target'),"
                "pg_database_size(current_database()) "
                "FROM dlr_subscription_target "
                "WHERE starts_with(business_id,%s)",
                (self.prefix + "-",),
            ).fetchone()
        (
            facts["target_count"],
            facts["target_writes"],
            facts["target_relation_bytes"],
            facts["target_database_bytes"],
        ) = target
        if self.config.get("resource_command"):
            result = subprocess.run(
                self.config["resource_command"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("resource_inspection_failed")
            resources = json.loads(result.stdout)
            facts["resources"] = {
                key: value
                for key, value in resources.items()
                if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            }
        return {"at": utc(), "elapsed": time.monotonic() - self.start, **facts}

    def collect(self) -> None:
        next_snapshot = 0.0
        try:
            while not self.stop.is_set():
                tick = time.monotonic()
                self.api_sample()
                if tick >= next_snapshot:
                    row = self.snapshot()
                    self.samples.append(row)
                    self.append("samples", row)
                    next_snapshot = tick + 10
                self.stop.wait(max(0, tick + 1 - time.monotonic()))
        except Exception as error:  # noqa: BLE001 - collector failure must stop publication.
            self.collector_error = type(error).__name__
            self.stop.set()

    def require_fresh(self, events: list[dict[str, Any]]) -> None:
        identifiers = [item["business_id"] for item in events]
        with psycopg.connect(self.config["target_database_url"], connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout='5s'")
            count = connection.execute(
                "SELECT count(*) FROM dlr_subscription_target WHERE business_id=ANY(%s)",
                (identifiers,),
            ).fetchone()[0]
        subs = ",".join(str(x["subscription_id"]) for x in self.scenarios)
        existing = self.sql(f"""SELECT coalesce(json_agg(x),'[]') FROM
          (SELECT (convert_from(decode(envelope#>>'{{data,value}}','base64'),'UTF8')::jsonb)
            ->>'business_id' AS business_id FROM event_receipts
           WHERE subscription_id IN ({subs})) x""")
        if count or set(identifiers).intersection(x["business_id"] for x in existing):
            raise RuntimeError("experiment_business_id_not_fresh")

    def run(self, seconds: int, rate: float, size: int, start_sequence: int) -> dict[str, Any]:
        if not 0 < size <= 4096 or not 0 <= rate <= 10:
            raise ValueError("invalid_representative_load")
        if self.phase == "steady" and (
            len(self.scenarios) != 1 or self.scenarios[0]["partitions"] != 4
        ):
            raise ValueError("steady_requires_one_four_partition_fixture")
        if rate:
            self.require_fresh(
                [
                    payload(self.prefix, start_sequence + index, size)
                    for index in range(math.ceil(seconds * rate))
                ]
            )
        self.start = time.monotonic()
        self.write(
            "basis",
            {
                "phase": self.phase,
                "started_at": utc(),
                "duration_seconds": seconds,
                "rate_per_second": rate,
                "business_payload_bytes": size,
                "scenarios": self.scenarios,
                "source_commit": self.config["source_commit"],
                "measurements": "real Control/Rabbit/Worker and independent target PostgreSQL",
            },
        )
        collector = threading.Thread(target=self.collect, name="capacity-measurement", daemon=True)
        collector.start()
        producer: KafkaProducer | None = None
        try:
            producer = KafkaProducer(**self.config["producer"]) if rate else None
            for index in range(math.ceil(seconds * rate)) if rate else ():
                if self.stop.wait(max(0, self.start + index / rate - time.monotonic())):
                    raise RuntimeError("measurement_collector_failed")
                sequence = start_sequence + index
                case = self.scenarios[index % len(self.scenarios)]
                item = payload(self.prefix, sequence, size)
                raw = encoded(item)
                partition = (index // len(self.scenarios)) % case["partitions"]
                if "partition" in case:
                    partition = case["partition"]
                sent_at = utc()
                assert producer is not None
                metadata = producer.send(
                    case["topic"],
                    partition=partition,
                    key=item["business_id"].encode(),
                    value=raw,
                ).get(timeout=10)
                row = {
                    "business_id": item["business_id"],
                    "sequence": sequence,
                    "subscription_id": case["subscription_id"],
                    "adapter_id": case["adapter_id"],
                    "partition": metadata.partition,
                    "topic": metadata.topic,
                    "offset": metadata.offset,
                    "sent_at": sent_at,
                    "payload_bytes": len(raw),
                    "payload_sha256": hashlib.sha256(raw).hexdigest(),
                    "schedule_lateness_seconds": max(
                        0, time.monotonic() - (self.start + index / rate)
                    ),
                }
                self.events.append(row)
                self.append("sent", row)
            self.stop.wait(max(0, self.start + seconds - time.monotonic()))
        finally:
            if producer is not None:
                producer.close(timeout=5)
            self.stop.set()
            collector.join(timeout=20)
            self.write("manifest", self.events)
        if self.collector_error or collector.is_alive():
            raise RuntimeError("measurement_collector_failed")
        row = self.snapshot()
        self.samples.append(row)
        self.append("samples", row)
        result = {
            "phase": self.phase,
            "elapsed_seconds": time.monotonic() - self.start,
            "sent_count": len(self.events),
            "sample_count": len(self.samples),
            "target_count_at_end": row["target_count"],
            "api": {
                route: {
                    "requests": len(values := [x for x in self.http if x["route"] == route]),
                    "p95_seconds": percentile([x["seconds"] for x in values], 0.95),
                    "non_200": sum(x["status"] != 200 for x in values),
                    "resource_5xx": sum(x["status"] >= 500 for x in values),
                }
                for route in sorted({x["route"] for x in self.http})
            },
            "note": "Completion, identity and side effects require the separate oracle phase.",
        }
        self.write("summary", result)
        return result

    def oracle(self, manifests: list[Path]) -> dict[str, Any]:
        events = [item for path in manifests for item in json.loads(path.read_text())]
        if not events or len({x["business_id"] for x in events}) != len(events):
            raise ValueError("oracle_requires_nonempty_unique_business_ids")
        subs = ",".join(str(x["subscription_id"]) for x in self.scenarios)
        receipts = self.sql(f"""SELECT coalesce(json_agg(x),'[]') FROM
          (SELECT r.id,r.subscription_id,r.source_event_id,r.state,r.payload_bytes,
            d.state AS delivery_state,e.status AS execution_state,
            e.created_at AS execution_created_at,e.ended_at AS execution_ended_at,
            e.attempt_count,
            (SELECT min(a.claimed_at) FROM execution_attempts a
             WHERE a.execution_id=e.id) AS first_claimed_at,
            r.first_received_at,r.terminal_at,r.envelope#>>'{{source,position,topic}}' AS topic,
            r.envelope#>>'{{source,position,partition}}' AS partition,
            r.envelope#>>'{{source,position,offset}}' AS offset,
            (convert_from(decode(r.envelope#>>'{{data,value}}','base64'),'UTF8')::jsonb)
              ->>'business_id' AS business_id
           FROM event_receipts r LEFT JOIN LATERAL
             (SELECT state,execution_id FROM event_deliveries
              WHERE receipt_id=r.id ORDER BY ordinal DESC LIMIT 1) d ON true
           LEFT JOIN executions e ON e.id=d.execution_id
           WHERE r.subscription_id IN ({subs})) x""")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in receipts:
            grouped.setdefault(item["business_id"], []).append(item)
        target: dict[str, Any] = {}
        with psycopg.connect(self.config["target_database_url"], connect_timeout=5) as connection:
            connection.execute("SET LOCAL statement_timeout='10s'")
            for ident, content, source_id, writes, created_at in connection.execute(
                "SELECT business_id,payload,source_event_id,writes,created_at "
                "FROM dlr_subscription_target "
                "WHERE business_id=ANY(%s)",
                ([x["business_id"] for x in events],),
            ):
                target[ident] = {
                    "payload": content,
                    "source_event_id": source_id,
                    "writes": writes,
                    "created_at": created_at.isoformat(),
                }
        failures = []
        latencies = []
        queue_delays = []
        projection_delays = []
        for event in events:
            ident = event["business_id"]
            row = target.get(ident)
            matches = grouped.get(ident, [])
            receipt = matches[0] if len(matches) == 1 else None
            if row is None:
                failures.append({"business_id": ident, "code": "target_missing"})
                continue
            expected = payload(self.prefix, event["sequence"], event["payload_bytes"])
            if row["payload"] != expected or row["writes"] != 1:
                failures.append({"business_id": ident, "code": "target_content_or_effect_mismatch"})
            if receipt is None or row["source_event_id"] != receipt["source_event_id"]:
                failures.append({"business_id": ident, "code": "source_identity_mismatch"})
            elif receipt["state"] != "succeeded":
                failures.append({"business_id": ident, "code": "receipt_not_succeeded"})
            elif (
                receipt["delivery_state"] != "succeeded"
                or receipt["execution_state"] != "succeeded"
            ):
                failures.append({"business_id": ident, "code": "execution_not_succeeded"})
            elif receipt["subscription_id"] != event["subscription_id"]:
                failures.append({"business_id": ident, "code": "subscription_mismatch"})
            elif receipt["payload_bytes"] > 4096:
                failures.append({"business_id": ident, "code": "representative_envelope_too_large"})
            elif receipt["topic"] != event["topic"]:
                failures.append({"business_id": ident, "code": "source_topic_mismatch"})
            elif (int(receipt["partition"]), int(receipt["offset"])) != (
                event["partition"],
                event["offset"],
            ):
                failures.append({"business_id": ident, "code": "source_position_mismatch"})
            latencies.append(
                (
                    datetime.fromisoformat(row["created_at"])
                    - datetime.fromisoformat(event["sent_at"])
                ).total_seconds()
            )
            if receipt is not None and receipt["first_claimed_at"]:
                queue_delays.append(
                    (
                        datetime.fromisoformat(receipt["first_claimed_at"])
                        - datetime.fromisoformat(receipt["execution_created_at"])
                    ).total_seconds()
                )
            if receipt is not None and receipt["terminal_at"] and receipt["execution_ended_at"]:
                projection_delays.append(
                    (
                        datetime.fromisoformat(receipt["terminal_at"])
                        - datetime.fromisoformat(receipt["execution_ended_at"])
                    ).total_seconds()
                )
        for case in self.scenarios:
            for part in range(case["partitions"]):
                ordered = sorted(
                    (
                        x
                        for x in events
                        if x["subscription_id"] == case["subscription_id"]
                        and x["partition"] == part
                        and x["business_id"] in target
                    ),
                    key=lambda x: x["offset"],
                )
                times = [target[x["business_id"]]["created_at"] for x in ordered]
                if times != sorted(times):
                    failures.append(
                        {
                            "code": "target_partition_order_mismatch",
                            "subscription_id": case["subscription_id"],
                            "partition": part,
                        }
                    )
        self.write(
            "oracle-detail",
            {
                "sent": events,
                "receipts": receipts,
                "target": target,
                "failures": failures,
            },
        )
        result = {
            "published": len(events),
            "target_records": len(target),
            "failures": len(failures),
            "all_full_payload_source_position_identity_and_one_effect_checked": not failures,
            "end_to_end_p50_seconds": percentile(latencies, 0.5),
            "end_to_end_p95_seconds": percentile(latencies, 0.95),
            "end_to_end_max_seconds": max(latencies) if latencies else None,
            "initial_execution_queue_p95_seconds": percentile(queue_delays, 0.95),
            "initial_execution_queue_max_seconds": max(queue_delays) if queue_delays else None,
            "terminal_projection_p95_seconds": percentile(projection_delays, 0.95),
            "terminal_projection_max_seconds": max(projection_delays)
            if projection_delays
            else None,
            "executions_with_retries": sum((x["attempt_count"] or 0) > 1 for x in receipts),
            "final_envelope_bytes_min": min((x["payload_bytes"] for x in receipts), default=None),
            "final_envelope_bytes_max": max((x["payload_bytes"] for x in receipts), default=None),
            "remaining_pending_processing": sum(
                x["state"] in {"pending", "processing"} for x in receipts
            ),
        }
        self.write("summary", result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--phase", choices=[*PHASE_SECONDS, "burst", "oracle"], required=True)
    parser.add_argument("--burst-seconds", type=int, default=10)
    parser.add_argument("--payload-bytes", type=int, default=2048)
    parser.add_argument("--start-sequence", type=int, default=0)
    parser.add_argument("--manifest", type=Path, action="append", default=[])
    args = parser.parse_args()
    probe = Probe(json.loads(args.config.read_text()), args.phase)
    if args.phase == "oracle":
        result = probe.oracle(args.manifest)
    else:
        if not 1 <= args.burst_seconds <= 60:
            raise ValueError("burst_must_be_finite")
        seconds = args.burst_seconds if args.phase == "burst" else PHASE_SECONDS[args.phase]
        rate = 10 if args.phase == "burst" else 1 if args.phase in {"steady", "warmup"} else 0
        result = probe.run(seconds, rate, args.payload_bytes, args.start_sequence)
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.phase == "oracle" and result["failures"]:
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - SDK errors can contain credentials or payloads.
        # Avoid echoing SDK exceptions, SQL, argv or configuration-bearing errors.
        print(json.dumps({"probe_failed": True, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
