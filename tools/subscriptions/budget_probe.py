#!/usr/bin/env python3
"""Isolated B0 feasibility probe; this does not exercise the DLR product runtime.

Run with the backend's Python environment. Results contain measurements, assertions,
and logical quota estimates, never connection configuration or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import importlib.metadata
import json
import math
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI

MIB = 1024 * 1024


def rss_bytes() -> int:
    """Current RSS, rather than the process lifetime high-water mark."""
    if sys.platform == "linux":
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    return (
        int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())]))
        * 1024
    )


class BoundedPool:
    """No waiting submission queue; release capacity on native call completion.

    Cancelling an asyncio wrapper must never release the permit of a still-running
    concurrent Future. Client timeouts/close cooperation remain mandatory.
    """

    def __init__(self, workers: int, application_bytes: int) -> None:
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="subscription-b0"
        )
        self.slots = threading.BoundedSemaphore(workers)
        self.lock = threading.Lock()
        self.byte_limit = application_bytes
        self.used_bytes = 0
        self.peak_bytes = 0
        self.active_connections = 0
        self.peak_connections = 0
        self.stop = threading.Event()

    def submit(
        self, address: tuple[str, int], timeout: float, buffer_bytes: int
    ) -> concurrent.futures.Future[dict[str, Any]]:
        if not self.slots.acquire(blocking=False):
            raise RuntimeError("listener_budget_exhausted")
        with self.lock:
            if self.used_bytes + buffer_bytes > self.byte_limit:
                self.slots.release()
                raise RuntimeError("application_buffer_exhausted")
            self.used_bytes += buffer_bytes
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)
        try:
            future = self.executor.submit(self._receive, address, timeout, buffer_bytes)
        except BaseException:
            with self.lock:
                self.used_bytes -= buffer_bytes
            self.slots.release()
            raise

        def completed(_: concurrent.futures.Future[dict[str, Any]]) -> None:
            with self.lock:
                self.used_bytes -= buffer_bytes
            self.slots.release()

        future.add_done_callback(completed)
        return future

    def _receive(
        self, address: tuple[str, int], timeout: float, buffer_bytes: int
    ) -> dict[str, Any]:
        start = time.perf_counter()
        buffer = bytearray(buffer_bytes)
        for offset in range(0, len(buffer), 4096):
            buffer[offset] = 1  # Make the application's reserved pages resident.
        timed_out = False
        with socket.create_connection(address, timeout=timeout) as connection:
            connection.settimeout(timeout)
            with self.lock:
                self.active_connections += 1
                self.peak_connections = max(
                    self.peak_connections, self.active_connections
                )
            try:
                connection.recv(1)  # Real bounded, blocking network call.
            except TimeoutError:
                timed_out = True
            finally:
                with self.lock:
                    self.active_connections -= 1
        return {
            "timed_out": timed_out,
            "elapsed_seconds": time.perf_counter() - start,
            "stop_observed_after_native_call": self.stop.is_set(),
        }


def quota_estimates(envelope_bytes: int) -> dict[str, Any]:
    day = 86400
    subscription_history = min(10_000, 256 * MIB // envelope_bytes)
    global_history = min(100_000, 1024 * MIB // envelope_bytes)
    subscription_pending = min(1_000, 32 * MIB // envelope_bytes)
    global_pending = min(10_000, 256 * MIB // envelope_bytes)
    estimates = []
    for rate in (0.01, 1.0, 10.0):
        estimates.append(
            {
                "events_per_second": rate,
                "events_per_day": rate * day,
                "envelope_bytes_per_day_lower_bound": rate * day * envelope_bytes,
                "envelope_bytes_7_days_lower_bound": rate * day * 7 * envelope_bytes,
                "dedupe_records_30_days": rate * day * 30,
                "subscription_history_full_seconds_without_expiry": subscription_history
                / rate,
                "global_history_full_seconds_at_total_rate_without_expiry": global_history
                / rate,
                "global_dedupe_full_seconds_at_total_rate_without_expiry": 500_000
                / rate,
                "subscription_pending_pause_seconds_if_target_stalled": min(
                    subscription_pending / rate, 1800
                ),
                "global_pending_full_seconds_at_total_rate_if_target_stalled": global_pending
                / rate,
            }
        )
    return {
        "scope": "Logical lower bounds only; PostgreSQL rows/indexes, WAL, logs, execution and "
        "outbox copies and native client buffers require measurements with the B3/B5 runtime.",
        "representative_final_envelope_bytes": envelope_bytes,
        "maximum_average_rate_per_subscription_for_7_day_raw_window": subscription_history
        / (7 * day),
        "maximum_average_global_rate_for_7_day_raw_window": global_history / (7 * day),
        "maximum_average_global_rate_for_30_day_dedupe_window": 500_000 / (30 * day),
        "estimates": estimates,
    }


async def probe(args: argparse.Namespace) -> dict[str, Any]:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    loop = asyncio.get_running_loop()
    # The only use of this executor is an explicit sentinel while listeners run.
    ordinary = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="ordinary-b0"
    )
    loop.set_default_executor(ordinary)
    sink_writers: set[asyncio.StreamWriter] = set()

    async def sink(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sink_writers.add(writer)
        try:
            await reader.read()
        finally:
            sink_writers.discard(writer)
            writer.close()
            await writer.wait_closed()

    sink_server = await asyncio.start_server(sink, "127.0.0.1", 0)
    sink_address = sink_server.sockets[0].getsockname()[:2]
    http_socket = socket.socket()
    http_socket.bind(("127.0.0.1", 0))
    http_socket.listen(128)
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="critical", lifespan="off", ws="none", access_log=False
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[http_socket]))
    pool = BoundedPool(args.listeners, args.application_mib * MIB)
    jobs: list[concurrent.futures.Future[dict[str, Any]]] = []
    assert args.application_mib * MIB % args.listeners == 0
    per_listener_buffer = args.application_mib * MIB // args.listeners
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.005)
        url = f"http://127.0.0.1:{http_socket.getsockname()[1]}/health"
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:

            async def sample(count: int) -> dict[str, Any]:
                timings: list[float] = []
                statuses: list[int] = []
                slots = asyncio.Semaphore(args.http_concurrency)

                async def request() -> None:
                    async with slots:
                        start = time.perf_counter()
                        response = await client.get(url)
                        statuses.append(response.status_code)
                        timings.append((time.perf_counter() - start) * 1000)

                await asyncio.gather(*(request() for _ in range(count)))
                timings.sort()
                return {
                    "requests": count,
                    "concurrency": args.http_concurrency,
                    "p50_ms": timings[math.ceil(0.5 * count) - 1],
                    "p95_ms": timings[math.ceil(0.95 * count) - 1],
                    "max_ms": max(timings),
                    "non_200_responses": sum(code != 200 for code in statuses),
                }

            await sample(30)
            baseline = await sample(args.requests)
            rss_baseline = rss_bytes()
            buffer_rejection = None
            try:
                pool.submit(sink_address, args.socket_timeout, pool.byte_limit + 1)
            except RuntimeError as error:
                buffer_rejection = str(error)
            jobs = [
                pool.submit(sink_address, args.socket_timeout, per_listener_buffer)
                for _ in range(args.listeners)
            ]
            async with asyncio.timeout(5):
                while pool.active_connections != args.listeners:
                    await asyncio.sleep(0.002)
            rss_blocked = rss_bytes()
            rejection = None
            try:
                pool.submit(sink_address, args.socket_timeout, 0)
            except RuntimeError as error:
                rejection = str(error)
            # A running native call cannot be killed by Future.cancel().
            native_cancel_returned = jobs[0].cancel()
            default_before = sum(
                t.name.startswith("ordinary-b0") for t in threading.enumerate()
            )
            sentinel_started = time.perf_counter()
            sentinel_thread = await asyncio.to_thread(
                lambda: threading.current_thread().name
            )
            sentinel_ms = (time.perf_counter() - sentinel_started) * 1000
            blocked = await sample(args.requests)
            active_after_http = pool.active_connections
            stop_started = time.perf_counter()
            pool.stop.set()
            outcomes = await asyncio.gather(*(asyncio.wrap_future(job) for job in jobs))
            stop_to_closed = time.perf_counter() - stop_started
            jobs = []
            rss_cycles = []
            # Reuse the same bounded threads/connections repeatedly with short timeouts.
            for _ in range(args.reuse_cycles):
                jobs = [
                    pool.submit(sink_address, 0.1, per_listener_buffer)
                    for _ in range(args.listeners)
                ]
                await asyncio.gather(*(asyncio.wrap_future(job) for job in jobs))
                jobs = []
                rss_cycles.append(rss_bytes())
            threads_before_shutdown = sum(
                thread.name.startswith("subscription-b0")
                for thread in threading.enumerate()
            )
            pool.executor.shutdown(wait=True, cancel_futures=True)
            await asyncio.sleep(0.02)
            threads_after_shutdown = sum(
                thread.name.startswith("subscription-b0")
                for thread in threading.enumerate()
            )
            ratio = blocked["p95_ms"] / baseline["p95_ms"]
            checks = {
                "http_p95_at_most_double_baseline": ratio <= 2,
                "no_http_errors": baseline["non_200_responses"]
                == blocked["non_200_responses"]
                == 0,
                "all_listeners_blocked_through_http_sample": active_after_http
                == args.listeners,
                "extra_listener_rejected": rejection == "listener_budget_exhausted",
                "oversized_buffer_reservation_rejected": buffer_rejection
                == "application_buffer_exhausted",
                "listener_calls_did_not_start_default_executor_threads": default_before
                == 0,
                "default_executor_sentinel_responsive": sentinel_ms < 1000
                and sentinel_thread.startswith("ordinary-b0"),
                "running_native_call_not_cancelled": native_cancel_returned is False,
                "all_native_calls_timeout": all(
                    outcome["timed_out"] for outcome in outcomes
                ),
                "stop_recovery_bounded_by_network_timeout": stop_to_closed
                <= args.socket_timeout + 0.5,
                "connections_bounded_and_closed": pool.peak_connections
                <= args.listeners
                and pool.active_connections == 0
                and len(sink_writers) == 0,
                "application_buffer_bounded_and_released": pool.peak_bytes
                <= args.application_mib * MIB
                and pool.used_bytes == 0,
                "dedicated_threads_bounded_and_reclaimed": threads_before_shutdown
                <= args.listeners
                and threads_after_shutdown == 0,
            }
            return {
                "scope": "Isolated minimal FastAPI on actual TCP, blocking stdlib socket clients; "
                "not DLR or Kafka/Kubernetes capacity acceptance. HTTP client shares server loop.",
                "versions": {
                    "python": platform.python_version(),
                    "platform": sys.platform,
                    "architecture": platform.machine(),
                    "cpu_count": os.cpu_count(),
                    **{
                        name: importlib.metadata.version(name)
                        for name in ("fastapi", "uvicorn", "httpx")
                    },
                },
                "parameters": vars(args)
                | {"output": str(args.output) if args.output else None},
                "http": {"baseline": baseline, "blocked": blocked, "p95_ratio": ratio},
                "pool": {
                    "native_cancel_returned": native_cancel_returned,
                    "default_executor_sentinel_ms": sentinel_ms,
                    "stop_to_all_closed_seconds": stop_to_closed,
                    "longest_native_call_seconds": max(
                        x["elapsed_seconds"] for x in outcomes
                    ),
                    "peak_connections": pool.peak_connections,
                    "application_buffer_peak_bytes": pool.peak_bytes,
                    "threads_before_shutdown": threads_before_shutdown,
                    "threads_after_shutdown": threads_after_shutdown,
                },
                "rss": {
                    "baseline_bytes": rss_baseline,
                    "all_listeners_blocked_bytes": rss_blocked,
                    "reuse_cycle_finished_bytes": rss_cycles,
                    "after_pool_shutdown_bytes": rss_bytes(),
                },
                "checks": checks,
                "passed": all(checks.values()),
                "quota_estimates": quota_estimates(args.envelope_bytes),
            }
    finally:
        pool.stop.set()
        if jobs:
            await asyncio.gather(
                *(asyncio.wrap_future(job) for job in jobs), return_exceptions=True
            )
        pool.executor.shutdown(wait=True, cancel_futures=True)
        server.should_exit = True
        await server_task
        sink_server.close()
        await sink_server.wait_closed()
        for writer in list(sink_writers):
            writer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listeners", type=int, default=16)
    parser.add_argument("--application-mib", type=int, default=64)
    parser.add_argument("--socket-timeout", type=float, default=3)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--http-concurrency", type=int, default=4)
    parser.add_argument("--reuse-cycles", type=int, default=5)
    parser.add_argument("--envelope-bytes", type=int, default=4096)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        min(
            args.listeners,
            args.application_mib,
            args.socket_timeout,
            args.requests,
            args.http_concurrency,
            args.reuse_cycles,
            args.envelope_bytes,
        )
        <= 0
    ):
        parser.error("all numeric parameters must be positive")
    if args.application_mib * MIB % args.listeners:
        parser.error("application buffer must divide evenly between listeners")
    result = asyncio.run(probe(args))
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
