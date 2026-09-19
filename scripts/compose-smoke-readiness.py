#!/usr/bin/env python3
"""Bounded startup readiness gate for the isolated Compose smoke."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HealthObservation:
    url: str
    status: int | None
    ready: bool
    reason: str


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _required_null(value: dict[str, Any], key: str, name: str) -> bool:
    if key not in value:
        raise ValueError(f"{name} is required")
    return value[key] is None


def health_payload_ready(payload: object) -> bool:
    """Validate the stable health wire shape and evaluate the startup contract."""
    root = _object(payload, "health")
    service = root.get("service")
    status = root.get("status")
    database = _strict_bool(root.get("database"), "database")
    outbox = _object(root.get("outbox"), "outbox")
    rabbitmq = _object(root.get("rabbitmq"), "rabbitmq")
    ingress = _object(rabbitmq.get("ingress"), "rabbitmq.ingress")
    repair = _object(rabbitmq.get("repair"), "rabbitmq.repair")
    worker_count = _positive_int(rabbitmq.get("worker_count"), "rabbitmq.worker_count")
    repair_worker_count = _positive_int(
        repair.get("worker_count"), "rabbitmq.repair.worker_count"
    )
    rabbitmq_error_clear = _required_null(
        rabbitmq, "last_error_code", "rabbitmq.last_error_code"
    )
    ingress_error_clear = _required_null(
        ingress, "last_error_code", "rabbitmq.ingress.last_error_code"
    )
    repair_error_clear = _required_null(
        repair, "last_error_code", "rabbitmq.repair.last_error_code"
    )
    return (
        service == "dlr-control"
        and status == "ok"
        and database
        and outbox.get("status") == "ok"
        and _strict_bool(rabbitmq.get("enabled"), "rabbitmq.enabled")
        and rabbitmq.get("status") == "ready"
        and _strict_bool(rabbitmq.get("ready"), "rabbitmq.ready")
        and rabbitmq_error_clear
        and _strict_bool(ingress.get("enabled"), "rabbitmq.ingress.enabled")
        and ingress.get("status") == "ready"
        and _strict_bool(ingress.get("ready"), "rabbitmq.ingress.ready")
        and ingress_error_clear
        and _strict_bool(repair.get("configured"), "rabbitmq.repair.configured")
        and repair.get("status") == "ready"
        and _strict_bool(repair.get("ready"), "rabbitmq.repair.ready")
        and repair_error_clear
        and worker_count == repair_worker_count
    )


def fetch_health(url: str, timeout: float) -> HealthObservation:
    effective_timeout = max(0.001, timeout)
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        f"{effective_timeout:.6f}",
        "--connect-timeout",
        f"{min(2.0, effective_timeout):.6f}",
        "--max-filesize",
        "65536",
        "--output",
        "-",
        "--write-out",
        "\n%{http_code}",
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=effective_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return HealthObservation(url, None, False, type(error).__name__)
    body, separator, status_text = completed.stdout.rpartition(b"\n")
    status = int(status_text) if separator and status_text.isdigit() else None
    if completed.returncode != 0 or status != 200:
        return HealthObservation(
            url, status, False, f"curl_exit_{completed.returncode}"
        )
    try:
        payload = json.loads(body)
        ready = health_payload_ready(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        return HealthObservation(url, status, False, type(error).__name__)
    return HealthObservation(url, status, ready, "ready" if ready else "not_ready")


def compose_services_healthy(
    project: str, services: Sequence[str], timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    for service in services:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            container = subprocess.run(
                ["docker", "compose", "-p", project, "ps", "-q", service],
                check=False,
                capture_output=True,
                text=True,
                timeout=min(5, remaining),
            )
        except subprocess.TimeoutExpired:
            return False
        container_id = container.stdout.strip()
        if container.returncode != 0 or not container_id:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            health = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Health.Status}}",
                    container_id,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=min(5, remaining),
            )
        except subprocess.TimeoutExpired:
            return False
        if health.returncode != 0 or health.stdout.strip() != "healthy":
            return False
    return True


def wait_for_startup(
    *,
    timeout: float,
    urls: Sequence[str],
    services_healthy: Callable[[float], bool],
    observe: Callable[[str, float], HealthObservation] = fetch_health,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.5,
    request_timeout: float = 3.0,
) -> list[HealthObservation]:
    deadline = monotonic() + timeout
    last: list[HealthObservation] = []
    while monotonic() < deadline:
        remaining = deadline - monotonic()
        if services_healthy(remaining):
            current: list[HealthObservation] = []
            for url in urls:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                current.append(observe(url, min(request_timeout, remaining)))
                if monotonic() >= deadline:
                    break
            last = current
            if (
                len(current) == len(urls)
                and all(item.ready for item in current)
                and monotonic() < deadline
            ):
                return current
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(interval, remaining))
    detail = (
        ", ".join(
            f"{item.url}:http={item.status}:reason={item.reason}" for item in last
        )
        or "no live health observations"
    )
    raise TimeoutError(
        f"Compose startup did not become ready within {timeout:g}s ({detail})"
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project", required=True)
    result.add_argument("--service", action="append", required=True)
    result.add_argument("--url", action="append", required=True)
    result.add_argument("--timeout", type=float, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.timeout <= 0 or len(args.url) != 2:
        raise SystemExit(
            "startup timeout must be positive and exactly two health URLs are required"
        )
    try:
        wait_for_startup(
            timeout=args.timeout,
            urls=args.url,
            services_healthy=lambda remaining: compose_services_healthy(
                args.project, args.service, remaining
            ),
        )
    except TimeoutError as error:
        print(f"ERROR: {error}")
        return 1
    print("Compose services and both live Control health paths are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
