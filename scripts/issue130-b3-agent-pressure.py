#!/usr/bin/env python3
"""Drive the real Compose Worker Agent through the v3 Control/RabbitMQ path.

This is a task-owned external pressure driver.  It must run in a separate
probe container on the Compose network; it never runs inside the Worker
container and never calls the in-process ``run_one`` diagnostic.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "worker_lost",
    "resource_exceeded",
}
PRESSURE_LEAD_SECONDS = 20.0
PRESSURE_SUSTAIN_SECONDS = 24.0
ATTEMPT_TIMEOUT_SECONDS = 60
PROFILE_LIMIT_KEYS = (
    "cpu_cores",
    "memory_bytes",
    "pids",
    "tmp_bytes",
    "nofile",
    "execution_timeout_seconds",
    "claim_timeout_seconds",
    "recovery_grace_seconds",
    "workspace_cleanup_attempt_timeout_seconds",
    "workspace_cleanup_total_timeout_seconds",
    "stream_max_bytes",
    "output_max_bytes",
    "output_preview_max_bytes",
)


def _api_request(
    method: str,
    path: str,
    payload: object | None = None,
    *,
    expected: int = 200,
    timeout: float = 5,
) -> object:
    token = os.environ.get("DLR_ADMIN_TOKEN")
    if not token:
        raise AssertionError("agent pressure driver requires the Control admin token")
    base_url = os.environ.get("DLR_CONTROL_URL", "http://control:8000").rstrip("/")
    body = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
    )
    request = urllib.request.Request(f"{base_url}/api{path}", data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read(64 * 1024)
    try:
        result = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeError, ValueError) as error:
        raise AssertionError(f"Control API returned invalid JSON for {method} {path}") from error
    if status != expected:
        raise AssertionError(f"Control API {method} {path} returned HTTP {status}")
    return result


def _health_sample() -> dict[str, object]:
    base_url = os.environ.get("DLR_CONTROL_URL", "http://control:8000").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as response:
            body = json.loads(response.read(64 * 1024).decode("utf-8"))
            return {
                "http_status": response.status,
                "status": body.get("status") if isinstance(body, dict) else None,
                "database": body.get("database") if isinstance(body, dict) else None,
                "rabbitmq": body.get("rabbitmq") if isinstance(body, dict) else None,
                "outbox": body.get("outbox") if isinstance(body, dict) else None,
            }
    except (OSError, urllib.error.URLError, TimeoutError, ValueError, UnicodeError) as error:
        return {"http_status": None, "error": type(error).__name__}


def _healthy(sample: dict[str, object]) -> bool:
    rabbitmq = sample.get("rabbitmq")
    outbox = sample.get("outbox")
    if not isinstance(rabbitmq, dict) or not isinstance(outbox, dict):
        return False
    repair = rabbitmq.get("repair")
    ingress = rabbitmq.get("ingress")
    return (
        sample.get("http_status") == 200
        and sample.get("status") == "ok"
        and sample.get("database") is True
        and outbox.get("status") == "ok"
        and isinstance(repair, dict)
        and repair.get("ready") is True
        and isinstance(ingress, dict)
        and ingress.get("ready") is True
    )


def _wait_for_agent() -> tuple[dict[str, object], dict[str, object]]:
    worker_name = os.environ.get("DLR_WORKER_NAME", "worker-1")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        workers = _api_request("GET", "/workers")
        sample = _health_sample()
        if isinstance(workers, list):
            for worker in workers:
                if (
                    isinstance(worker, dict)
                    and worker.get("name") == worker_name
                    and worker.get("status") == "online"
                    and worker.get("protocol_version") == 3
                    and worker.get("rabbitmq_execution_v3") is True
                    and isinstance(worker.get("isolation_capabilities"), dict)
                    and worker["isolation_capabilities"].get("resource_envelope_verified") is True
                    and _healthy(sample)
                ):
                    return worker, sample
        time.sleep(0.5)
    raise AssertionError("real Compose Worker Agent did not pass the v3 registration gate")


def _create_canary(
    worker_id: int,
    suffix: str,
    language: str,
    code: str,
    *,
    pressure_start_unix: float,
) -> tuple[int, int]:
    adapter = _api_request(
        "POST",
        "/adapters",
        {
            "name": f"dlr-b3-f3-agent-{suffix}",
            "description": "Issue 130 B3 real Worker Agent pressure proof",
            "language": language,
            "adapter_type": "task",
            "timeout_seconds": ATTEMPT_TIMEOUT_SECONDS,
        },
        expected=201,
    )
    if not isinstance(adapter, dict) or not isinstance(adapter.get("id"), int):
        raise AssertionError("Control did not return an Adapter id")
    adapter_id = int(adapter["id"])
    version = _api_request(
        "POST",
        f"/adapters/{adapter_id}/versions",
        {"code": code, "requirements": "", "runtime_config": {}},
        expected=201,
    )
    if not isinstance(version, dict) or not isinstance(version.get("id"), int):
        raise AssertionError("Control did not return a Version id")
    selected = _api_request("PATCH", f"/adapters/{adapter_id}", {"runtime_worker_id": worker_id})
    if not isinstance(selected, dict) or selected.get("runtime_worker_id") != worker_id:
        raise AssertionError("Control did not bind the canary Adapter to the real Worker")
    execution = _api_request(
        "POST",
        f"/adapters/{adapter_id}/executions/canary",
        {
            "input": {
                "source": "issue130-b3-real-agent",
                "pressure_start_unix": pressure_start_unix,
            }
        },
        expected=202,
    )
    if not isinstance(execution, dict) or not isinstance(execution.get("id"), int):
        raise AssertionError("Control did not return an Execution id")
    if execution.get("dispatch_backend") != "rabbitmq" or execution.get("status") != "queued":
        raise AssertionError("canary did not enter the RabbitMQ dispatch path")
    return adapter_id, int(execution["id"])


def _pressure_specs() -> list[tuple[str, str, str]]:
    wait_for_pressure = (
        "    while time.time() < float(input['pressure_start_unix']):\n        time.sleep(0.02)\n"
    )
    return [
        (
            "python",
            "cpu",
            "import os\nimport time\n"
            "def handle(context, input):\n"
            f"{wait_for_pressure}"
            "    for _ in range(4):\n"
            "        if os.fork() == 0:\n"
            "            while True:\n"
            "                pass\n"
            "    while True:\n"
            "        pass\n",
        ),
        (
            "python",
            "memory",
            "import time\ndef handle(context, input):\n"
            f"{wait_for_pressure}"
            "    blocks = [bytearray(4 * 1024 * 1024) for _ in range(2)]\n"
            "    for block in blocks:\n"
            "        for offset in range(0, len(block), 4096):\n"
            "            block[offset] = 1\n"
            f"    while time.time() < float(input['pressure_start_unix']) + {PRESSURE_SUSTAIN_SECONDS!r}:\n"
            "        for block in blocks:\n"
            "            for offset in range(0, len(block), 4096):\n"
            "                block[offset] = (block[offset] + 1) % 255\n"
            "        time.sleep(0.02)\n"
            "    while True:\n"
            "        block = bytearray(4 * 1024 * 1024)\n"
            "        for offset in range(0, len(block), 4096):\n"
            "            block[offset] = 1\n"
            "        blocks.append(block)\n"
            "        time.sleep(0.05)\n",
        ),
    ]


def _positive_counter(value: object, key: str) -> int:
    if not isinstance(value, dict):
        raise AssertionError(f"resource usage did not include {key} counters")
    counter = value.get(key)
    if not isinstance(counter, int) or isinstance(counter, bool) or counter <= 0:
        raise AssertionError(f"resource counter {key} did not prove pressure")
    return counter


def _resource_evidence(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    observed_kinds: set[str] = set()
    for row in rows:
        attempts = row["attempts"]
        if not isinstance(attempts, list) or len(attempts) != 1:
            raise AssertionError("pressure Execution did not have exactly one terminal Attempt")
        attempt = attempts[0]
        profile = row["resource_profile"]
        if not isinstance(attempt, dict) or not isinstance(profile, dict):
            raise AssertionError("pressure Attempt or Resource Profile was malformed")
        usage = attempt.get("resource_usage_json")
        if not isinstance(usage, dict):
            raise AssertionError("terminal Attempt did not preserve cgroup resource usage")
        limits = usage.get("limits")
        if not isinstance(limits, dict) or any(
            limits.get(key) != profile.get(key) for key in PROFILE_LIMIT_KEYS
        ):
            raise AssertionError("resource usage limits did not match the queued profile snapshot")
        tmpfs = usage.get("tmpfs")
        if (
            not isinstance(tmpfs, dict)
            or tmpfs.get("bounded") is not True
            or tmpfs.get("limit") != profile.get("tmp_bytes")
        ):
            raise AssertionError("Attempt tmpfs usage was not bounded by its snapshot")

        kind = row["kind"]
        observed_kinds.add(str(kind))
        if kind == "cpu":
            cpu = usage.get("cpu")
            counters = {
                "usage_usec": _positive_counter(cpu, "usage_usec"),
                "nr_periods": _positive_counter(cpu, "nr_periods"),
                "nr_throttled": _positive_counter(cpu, "nr_throttled"),
                "throttled_usec": _positive_counter(cpu, "throttled_usec"),
            }
            if attempt.get("status") not in {"cancelled", "timed_out"}:
                raise AssertionError("CPU pressure Attempt had an unexpected terminal status")
            evidence.append({"kind": kind, "cgroup_cpu": counters})
        elif kind == "memory":
            memory = usage.get("memory")
            if not isinstance(memory, dict):
                raise AssertionError("memory pressure did not include cgroup memory usage")
            events = memory.get("events")
            if not isinstance(events, dict) or not any(
                isinstance(events.get(key), int)
                and not isinstance(events.get(key), bool)
                and int(events[key]) > 0
                for key in ("oom", "oom_kill", "oom_group_kill")
            ):
                raise AssertionError("memory pressure did not trigger a cgroup OOM event")
            if (
                attempt.get("status") != "resource_exceeded"
                or attempt.get("error_code") != "resource_exceeded_memory"
            ):
                raise AssertionError("memory pressure did not map to the stable resource error")
            evidence.append(
                {
                    "kind": kind,
                    "cgroup_memory": {
                        "peak": memory.get("peak"),
                        "events": events,
                    },
                }
            )
        else:
            raise AssertionError("unknown pressure kind")
    if observed_kinds != {"cpu", "memory"}:
        raise AssertionError("pressure proof must exercise both CPU and Memory limits")
    return evidence


def _record_attempt(row: dict[str, Any], execution: dict[str, Any], detail: dict[str, Any]) -> None:
    status = execution.get("status")
    if status not in row["status_history"]:
        row["status_history"].append(status)
    if row["resource_profile"] is None:
        row["resource_profile"] = execution.get("resource_profile_snapshot")
    attempts = detail.get("attempts")
    if not isinstance(attempts, list):
        raise AssertionError("reliable detail did not include Attempt rows")
    row["current_attempts"] = attempts
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        attempt_id = attempt.get("id")
        if isinstance(attempt_id, int) and attempt_id not in row["attempt_ids"]:
            row["attempt_ids"].append(attempt_id)
        attempt_status = attempt.get("status")
        if attempt_status not in row["attempt_status_history"]:
            row["attempt_status_history"].append(attempt_status)
        lease = attempt.get("lease_expires_at")
        if lease is not None and lease not in row["lease_expiries"]:
            row["lease_expiries"].append(lease)
    if attempts and all(
        isinstance(attempt, dict) and attempt.get("status") in TERMINAL_STATUSES
        for attempt in attempts
    ):
        row["attempts"] = attempts


def run_pressure() -> dict[str, object]:
    try:
        slots = int(os.environ.get("DLR_WORKER_EXECUTION_SLOTS", "4"))
    except ValueError as error:
        raise AssertionError("configured slots must be an integer") from error
    if slots < 2:
        raise AssertionError("real all-slot pressure requires at least two configured slots")
    envelope_raw = os.environ.get("DLR_B3_VERIFIED_ENVELOPE_JSON")
    if not envelope_raw:
        raise AssertionError("driver requires the envelope read from the same delegated parent")
    try:
        envelope = json.loads(envelope_raw)
    except json.JSONDecodeError as error:
        raise AssertionError("driver envelope is not JSON") from error
    if not isinstance(envelope, dict) or not envelope.get("source"):
        raise AssertionError("driver envelope is not a finite verified snapshot")

    worker, ready_sample = _wait_for_agent()
    worker_id = worker.get("id")
    if not isinstance(worker_id, int):
        raise AssertionError("real Agent readiness response did not include a Worker id")
    specs = _pressure_specs()
    pressure_start_unix = time.time() + PRESSURE_LEAD_SECONDS
    rows: list[dict[str, Any]] = []
    for index in range(slots):
        language, kind, code = specs[index % len(specs)]
        adapter_id, execution_id = _create_canary(
            worker_id,
            f"{os.getpid()}-{uuid.uuid4().hex[:10]}-{index + 1}",
            language,
            code,
            pressure_start_unix=pressure_start_unix,
        )
        rows.append(
            {
                "adapter_id": adapter_id,
                "execution_id": execution_id,
                "language": language,
                "kind": kind,
                "status_history": [],
                "attempt_status_history": [],
                "lease_expiries": [],
                "attempt_ids": [],
                "attempts": [],
                "current_attempts": [],
                "resource_profile": None,
            }
        )

    heartbeat_values: list[object] = []
    pressure_heartbeat_values: list[object] = []
    full_pressure_heartbeat_values: list[object] = []
    health_samples = [ready_sample]
    pressure_health_samples: list[dict[str, object]] = []
    full_pressure_health_samples: list[dict[str, object]] = []
    active_attempt_counts: list[int] = []
    max_active_attempts = 0
    max_active_attempts_during_limit_pressure = 0
    renewed_attempt_ids: list[int] = []
    renewed_during_full_pressure_attempt_ids: list[int] = []
    reported_attempt_ids: list[int] = []
    reported_during_pressure_attempt_ids: list[int] = []
    cancel_row: dict[str, Any] | None = None
    cancel_response: dict[str, Any] | None = None
    started_at = time.monotonic()
    deadline = started_at + 150
    while time.monotonic() < deadline:
        workers = _api_request("GET", "/workers")
        if isinstance(workers, list):
            current = next(
                (
                    item
                    for item in workers
                    if isinstance(item, dict) and item.get("id") == worker_id
                ),
                None,
            )
            if isinstance(current, dict):
                heartbeat_values.append(current.get("last_heartbeat"))
        sample = _health_sample()
        health_samples.append(sample)
        for row in rows:
            execution = _api_request("GET", f"/executions/{row['execution_id']}")
            detail = _api_request("GET", f"/executions/{row['execution_id']}/reliable-detail")
            if not isinstance(execution, dict) or not isinstance(detail, dict):
                raise AssertionError("Control returned malformed real-Agent pressure state")
            _record_attempt(row, execution, detail)
            if len(row["lease_expiries"]) >= 2:
                for attempt_id in row["attempt_ids"]:
                    if attempt_id not in renewed_attempt_ids:
                        renewed_attempt_ids.append(attempt_id)
            for attempt in row["current_attempts"]:
                if (
                    isinstance(attempt, dict)
                    and isinstance(attempt.get("id"), int)
                    and attempt.get("ended_at") is not None
                    and isinstance(attempt.get("cleanup_summary"), dict)
                    and attempt["cleanup_summary"].get("workspace_cleanup_status") == "completed"
                    and attempt["id"] not in reported_attempt_ids
                ):
                    reported_attempt_ids.append(attempt["id"])
        active = sum(
            1
            for row in rows
            for attempt in row["current_attempts"]
            if isinstance(attempt, dict) and attempt.get("status") in {"claimed", "running"}
        )
        active_attempt_counts.append(active)
        max_active_attempts = max(max_active_attempts, active)
        limit_pressure_started = time.time() >= pressure_start_unix
        if limit_pressure_started:
            max_active_attempts_during_limit_pressure = max(
                max_active_attempts_during_limit_pressure, active
            )
        if active and limit_pressure_started:
            pressure_health_samples.append(sample)
            pressure_heartbeat_values.extend(
                value for value in heartbeat_values[-1:] if value is not None
            )
            for attempt_id in reported_attempt_ids:
                if attempt_id not in reported_during_pressure_attempt_ids:
                    reported_during_pressure_attempt_ids.append(attempt_id)
        all_slots_under_pressure = limit_pressure_started and active == slots
        if all_slots_under_pressure:
            full_pressure_health_samples.append(sample)
            full_pressure_heartbeat_values.extend(
                value for value in heartbeat_values[-1:] if value is not None
            )
            for row in rows:
                if len(row["lease_expiries"]) >= 2:
                    for attempt_id in row["attempt_ids"]:
                        if attempt_id not in renewed_during_full_pressure_attempt_ids:
                            renewed_during_full_pressure_attempt_ids.append(attempt_id)
        if (
            cancel_row is None
            and max_active_attempts_during_limit_pressure >= slots
            and len([sample for sample in full_pressure_health_samples if _healthy(sample)]) >= 3
            and len({value for value in full_pressure_heartbeat_values if value is not None}) >= 2
            and renewed_during_full_pressure_attempt_ids
        ):
            cancel_row = next(
                (
                    row
                    for row in rows
                    if row["kind"] == "cpu" and "running" in row["attempt_status_history"]
                ),
                None,
            )
            if cancel_row is not None:
                response = _api_request("POST", f"/executions/{cancel_row['execution_id']}/cancel")
                if not isinstance(response, dict):
                    raise AssertionError("Control returned malformed cancel response")
                cancel_response = response
        if all(
            row["attempts"]
            and all(
                isinstance(attempt, dict) and attempt.get("status") in TERMINAL_STATUSES
                for attempt in row["attempts"]
            )
            for row in rows
        ):
            break
        time.sleep(0.25)
    else:
        raise AssertionError(
            {
                "error": "real Agent pressure did not reach terminal cleanup",
                "max_active_attempts": max_active_attempts,
                "heartbeats": len(heartbeat_values),
                "pressure_health_samples": len(pressure_health_samples),
                "attempts": [
                    {
                        "execution_id": row["execution_id"],
                        "status_history": row["status_history"],
                        "attempt_status_history": row["attempt_status_history"],
                        "lease_expiry_samples": len(row["lease_expiries"]),
                    }
                    for row in rows
                ],
            }
        )

    healthy_pressure = [sample for sample in pressure_health_samples if _healthy(sample)]
    healthy_full_pressure = [
        sample for sample in full_pressure_health_samples if _healthy(sample)
    ]
    heartbeat_updates = len({value for value in heartbeat_values if value is not None})
    pressure_heartbeat_updates = len(
        {value for value in pressure_heartbeat_values if value is not None}
    )
    full_pressure_heartbeat_updates = len(
        {value for value in full_pressure_heartbeat_values if value is not None}
    )
    if (
        max_active_attempts < slots
        or max_active_attempts_during_limit_pressure < slots
        or len(healthy_pressure) < 3
        or len(healthy_full_pressure) < 3
        or pressure_heartbeat_updates < 2
        or full_pressure_heartbeat_updates < 2
    ):
        raise AssertionError("Agent heartbeat/health evidence was insufficient during pressure")
    if cancel_row is None or cancel_response is None:
        raise AssertionError("real Agent cancel was not exercised")
    if not (
        cancel_response.get("cancel_requested") is True
        or cancel_response.get("status") == "cancelled"
    ):
        raise AssertionError("real Agent cancel response was not acknowledged")
    if not any("cancelled" in row["attempt_status_history"] for row in rows):
        raise AssertionError("real Agent cancellation did not reach an Attempt")
    if not any(
        set(row["attempt_status_history"]) & {"timed_out", "resource_exceeded"} for row in rows
    ):
        raise AssertionError("real Agent pressure did not exercise a bounded terminal result")
    if not renewed_attempt_ids:
        raise AssertionError("real Agent did not renew an Attempt during pressure")
    if not renewed_during_full_pressure_attempt_ids:
        raise AssertionError("real Agent did not renew while every slot was under pressure")
    if len(reported_attempt_ids) < slots:
        raise AssertionError("real Agent result reports/cleanup were not observed for every slot")
    if not reported_during_pressure_attempt_ids:
        raise AssertionError("real Agent did not report a result while pressure remained active")
    for row in rows:
        if not row["resource_profile"] or row["resource_profile"].get("backend") != "cgroup_v2":
            raise AssertionError("queued Resource Profile snapshot was not observed")
    resource_evidence = _resource_evidence(rows)
    return {
        "driver_role": "external_control_rabbitmq_pressure_driver",
        "driver_not_worker_exec": True,
        "actual_agent_is_f3_gate": True,
        "worker_id": worker_id,
        "worker_protocol_version": worker.get("protocol_version"),
        "rabbitmq_execution_v3": worker.get("rabbitmq_execution_v3"),
        "resource_envelope_verified_before_registration": worker.get(
            "isolation_capabilities", {}
        ).get("resource_envelope_verified"),
        "verified_envelope": envelope,
        "configured_slots": slots,
        "max_active_attempts": max_active_attempts,
        "max_active_attempts_during_limit_pressure": max_active_attempts_during_limit_pressure,
        "active_attempt_counts": active_attempt_counts,
        "heartbeat_samples": len(heartbeat_values),
        "heartbeat_updates": heartbeat_updates,
        "pressure_heartbeat_updates": pressure_heartbeat_updates,
        "full_pressure_heartbeat_updates": full_pressure_heartbeat_updates,
        "healthy_control_database_rabbitmq_outbox_samples": sum(
            1 for sample in health_samples if _healthy(sample)
        ),
        "pressure_healthy_control_database_rabbitmq_outbox_samples": len(healthy_pressure),
        "full_pressure_healthy_control_database_rabbitmq_outbox_samples": len(
            healthy_full_pressure
        ),
        "renewed_and_reported": bool(renewed_attempt_ids),
        "renewed_attempt_ids": renewed_attempt_ids,
        "renewed_during_full_pressure_attempt_ids": renewed_during_full_pressure_attempt_ids,
        "cancel_requested_and_reported": True,
        "cancel_response_status": cancel_response.get("status"),
        "result_reports": len(reported_attempt_ids),
        "result_reports_during_pressure": len(reported_during_pressure_attempt_ids),
        "result_report_attempt_ids_during_pressure": reported_during_pressure_attempt_ids,
        "resource_limits_proven": ["cpu", "memory"],
        "resource_evidence": resource_evidence,
        "attempts": [
            {
                "execution_id": row["execution_id"],
                "adapter_id": row["adapter_id"],
                "language": row["language"],
                "kind": row["kind"],
                "status_history": row["status_history"],
                "attempt_status_history": row["attempt_status_history"],
                "lease_expiry_samples": len(row["lease_expiries"]),
                "resource_profile": row["resource_profile"],
                "terminal_attempt": row["attempts"][0],
                "cleanup_completed": True,
            }
            for row in rows
        ],
    }


def main() -> int:
    print(json.dumps(run_pressure(), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "error": str(error),
                    "driver_role": "external_control_rabbitmq_pressure_driver",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
