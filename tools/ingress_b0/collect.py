#!/usr/bin/env python3
"""Read-only, bounded B0 sampling inside the isolated Linux platform.

resources uses a private manifest of container cgroup paths. ledger reads the
existing DLR database without changing ownership, transactions or task state.
Run each sampler independently; samples retain actual timestamps and overruns.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from pathlib import Path

RESOURCE_OUTPUT_LIMIT = 6 * 1024 * 1024
LEDGER_OUTPUT_LIMIT = 1024 * 1024


def read(path: Path, name: str) -> str | None:
    try:
        return (path / name).read_text().strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def pairs(value: str | None) -> dict[str, int]:
    return {k: int(v) for k, v in (line.split() for line in (value or "").splitlines())}


def process_rss(group: Path) -> tuple[int, int]:
    pids: set[int] = set()
    for item in [group, *group.rglob("*")]:
        if item.is_dir():
            pids.update(int(x) for x in (read(item, "cgroup.procs") or "").split())
    total = 0
    for pid in pids:
        value = read(Path(f"/proc/{pid}"), "status")
        for line in (value or "").splitlines():
            if line.startswith("VmRSS:"):
                total += int(line.split()[1]) * 1024
    return total, len(pids)


def resource_snapshot(config: dict) -> dict:
    components = {}
    for name, relative in config["cgroups"].items():
        root = Path("/sys/fs/cgroup").resolve()
        group = (root / relative.lstrip("/")).resolve()
        if not group.is_relative_to(root) or not group.is_dir():
            raise ValueError("invalid_or_missing_cgroup")
        rss, processes = process_rss(group)
        components[name] = {
            "memory_current": read(group, "memory.current"),
            "memory_peak": read(group, "memory.peak"),
            "memory_events": pairs(read(group, "memory.events")),
            "memory_max": read(group, "memory.max"),
            "memory_swap_max": read(group, "memory.swap.max"),
            "cpu_stat": pairs(read(group, "cpu.stat")),
            "cpu_max": read(group, "cpu.max"),
            "pids_current": read(group, "pids.current"),
            "io_stat": read(group, "io.stat"),
            "rss_bytes": rss,
            "process_count": processes,
        }
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        if k in {
            "MemTotal",
            "MemAvailable",
            "SwapTotal",
            "SwapFree",
            "Dirty",
            "Writeback",
        }:
            meminfo[k] = int(v.split()[0]) * 1024
    disk = os.statvfs(config.get("disk_path", "/var/lib/docker"))
    result = {
        "components": components,
        "vm_meminfo": meminfo,
        "vm_cpu_stat": Path("/proc/stat").read_text().splitlines()[0],
        "vm_loadavg": Path("/proc/loadavg").read_text().strip(),
        "disk_available_bytes": disk.f_bavail * disk.f_frsize,
        "pressure": {
            x: read(Path("/proc/pressure"), x) for x in ("cpu", "memory", "io")
        },
    }
    if config.get("clock_host"):
        try:
            with socket.create_connection(
                (config["clock_host"], int(config["clock_port"])), 0.5
            ) as sock:
                start = time.time_ns()
                sock.sendall(b"t")
                wire = bytearray()
                while b"\n" not in wire and len(wire) < 64:
                    chunk = sock.recv(64 - len(wire))
                    if not chunk:
                        break
                    wire.extend(chunk)
                end = time.time_ns()
                if not wire.endswith(b"\n") or not wire[:-1].isdigit():
                    raise ValueError("invalid_clock_reply")
                remote = int(wire)
                result["external_clock"] = {
                    "offset_ns": remote - (start + end) // 2,
                    "offset_error_bound_ns": (end - start) // 2,
                    "rtt_ns": end - start,
                }
        except Exception as error:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
            result["external_clock_error_type"] = type(error).__name__
    return result


def ledger_snapshot(config: dict) -> dict:
    from dlr.control.db import engine
    from sqlalchemy import text

    adapter_id = int(config["adapter_id"])
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT r.state, count(*) FROM event_receipts r "
                "JOIN event_subscriptions s ON s.id=r.subscription_id "
                "WHERE s.adapter_id=:a GROUP BY r.state"
            ),
            {"a": adapter_id},
        ).all()
        executions = conn.execute(
            text(
                "SELECT status,count(*) FROM executions WHERE adapter_id=:a GROUP BY status"
            ),
            {"a": adapter_id},
        ).all()
        attempts = conn.execute(
            text(
                "SELECT a.status,count(*) FROM execution_attempts a "
                "JOIN executions e ON e.id=a.execution_id "
                "WHERE e.adapter_id=:a GROUP BY a.status"
            ),
            {"a": adapter_id},
        ).all()
        locks = conn.execute(
            text(
                "SELECT wait_event_type,wait_event,count(*) FROM pg_stat_activity "
                "WHERE datname=current_database() GROUP BY wait_event_type,wait_event"
            )
        ).all()
    return {
        "receipt_counts": dict(rows),
        "execution_counts": dict(executions),
        "attempt_counts": dict(attempts),
        "database_waits": [list(x) for x in locks],
    }


def sample(mode: str, config: dict, output: Path, seconds: int) -> None:
    started = time.monotonic()
    size = 0
    limit = RESOURCE_OUTPUT_LIMIT if mode == "resources" else LEDGER_OUTPUT_LIMIT
    with os.fdopen(
        os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as log:
        while time.monotonic() - started < seconds:
            begin = time.monotonic()
            item = {
                "utc_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "mode": mode,
            }
            try:
                item.update(
                    resource_snapshot(config)
                    if mode == "resources"
                    else ledger_snapshot(config)
                )
            except Exception as error:  # noqa: BLE001 - keep diagnostics safe and redact driver errors
                item["sample_error_type"] = type(error).__name__
            item["sample_elapsed_ns"] = int((time.monotonic() - begin) * 1e9)
            data = json.dumps(item, separators=(",", ":")).encode() + b"\n"
            if size + len(data) > limit:
                raise RuntimeError("sample_output_budget_exhausted")
            log.write(data)
            log.flush()
            size += len(data)
            # Start-to-start cadence; a slow query is retained as an overrun.
            time.sleep(max(0.0, 1.0 - (time.monotonic() - begin)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("resources", "ledger"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=920)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error("sampling duration must be 1..1800 seconds")
    sample(args.mode, json.loads(args.config.read_text()), args.output, args.seconds)


if __name__ == "__main__":
    main()
