#!/usr/bin/env python3
"""Bounded startup readiness gate for the isolated Compose smoke."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
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
    return (
        service == "dlr-control"
        and status == "ok"
        and database
        and outbox.get("status") == "ok"
        and _strict_bool(rabbitmq.get("enabled"), "rabbitmq.enabled")
        and rabbitmq.get("status") == "ready"
        and _strict_bool(rabbitmq.get("ready"), "rabbitmq.ready")
        and rabbitmq.get("last_error_code") is None
        and _strict_bool(ingress.get("enabled"), "rabbitmq.ingress.enabled")
        and ingress.get("status") == "ready"
        and _strict_bool(ingress.get("ready"), "rabbitmq.ingress.ready")
        and ingress.get("last_error_code") is None
        and _strict_bool(repair.get("configured"), "rabbitmq.repair.configured")
        and repair.get("status") == "ready"
        and _strict_bool(repair.get("ready"), "rabbitmq.repair.ready")
        and repair.get("last_error_code") is None
        and worker_count == repair_worker_count
    )


def fetch_health(url: str, timeout: float) -> HealthObservation:
    status: int | None = None
    try:
        with urllib.request.urlopen(url, timeout=max(0.001, timeout)) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read()
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        return HealthObservation(url, None, False, type(error).__name__)
    try:
        payload = json.loads(body)
        ready = status == 200 and health_payload_ready(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        return HealthObservation(url, status, False, type(error).__name__)
    return HealthObservation(url, status, ready, "ready" if ready else "not_ready")


def compose_services_healthy(project: str, services: Sequence[str], timeout: float) -> bool:
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
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
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
            last = current
            if len(current) == len(urls) and all(item.ready for item in current):
                return current
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(interval, remaining))
    detail = ", ".join(
        f"{item.url}:http={item.status}:reason={item.reason}" for item in last
    ) or "no live health observations"
    raise TimeoutError(f"Compose startup did not become ready within {timeout:g}s ({detail})")


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
        raise SystemExit("startup timeout must be positive and exactly two health URLs are required")
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
