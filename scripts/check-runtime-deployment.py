#!/usr/bin/env python3
"""Validate rendered Compose policy without printing deployment credentials."""

from __future__ import annotations

import json
import sys
from typing import Any

REMOVED_SETTINGS = {
    "DLR_RABBITMQ_EXECUTION_ENABLED",
    "DLR_RABBITMQ_EXECUTION_CANARY_ENABLED",
    "DLR_WORKER_PROTOCOL_VERSION",
    "DLR_MIN_WORKER_PROTOCOL_VERSION",
    "DLR_LEGACY_EXECUTION_CLAIM_ENABLED",
    "DLR_CUTOVER_BACKUP_RESTORE_GATE_PASSED",
    "DLR_CUTOVER_SANDBOX_GATE_PASSED",
    "DLR_CUTOVER_SLOT_GATE_PASSED",
}


def validate_deployment(config: dict[str, Any]) -> None:
    services = config["services"]
    worker = services["worker"]
    if (
        worker.get("privileged", False) is not False
        or worker.get("cgroup") != "private"
    ):
        raise ValueError(
            "Worker requires privileged=false and a private cgroup namespace"
        )
    if set(worker.get("cap_add", [])) != {"SYS_ADMIN", "SETUID", "SETGID"}:
        raise ValueError("Worker supervisor capabilities differ from the required set")
    if worker.get("cap_drop") != ["ALL"] or worker.get("user") not in (
        None,
        "0",
        "0:0",
    ):
        raise ValueError("Worker requires the root supervisor with cap_drop=ALL")
    if set(worker.get("security_opt", [])) != {
        "no-new-privileges:true",
        "apparmor=unconfined",
    }:
        raise ValueError(
            "Worker requires no-new-privileges and the Worker-only AppArmor policy"
        )
    parent = worker.get("cgroup_parent", "")
    if not parent.startswith("/system.slice/dlr-") or not parent.endswith(".service"):
        raise ValueError("Worker requires a named DLR system-manager delegated unit")
    if "/../" in parent or parent.count("/") != 2:
        raise ValueError("Worker cgroup parent must identify exactly one unit")
    mounts = worker.get("volumes", [])
    delegated = [item for item in mounts if item.get("target") == "/run/dlr-cgroup"]
    if len(delegated) != 1:
        raise ValueError("Worker requires exactly one delegated cgroup bind")
    mount = delegated[0]
    if (
        mount.get("type") != "bind"
        or mount.get("source") != "/sys/fs/cgroup" + parent
        or mount.get("read_only", False)
        or mount.get("bind", {}).get("create_host_path", False)
    ):
        raise ValueError("Worker cgroup bind must match its exact prepared parent")
    if worker["environment"].get("DLR_SANDBOX_CGROUP_PATH") != "/run/dlr-cgroup":
        raise ValueError("Worker sandbox path must match the delegated bind")
    for service in services.values():
        if REMOVED_SETTINGS & service.get("environment", {}).keys():
            raise ValueError("Removed execution migration settings remain in Compose")
        for item in service.get("volumes", []):
            if item.get("source") in {"/", "/sys", "/sys/fs/cgroup"} or str(
                item.get("source", "")
            ).endswith("docker.sock"):
                raise ValueError(
                    "Broad host or Docker control-plane mount is not allowed"
                )
    if (
        services["control"]["depends_on"].get("rabbitmq", {}).get("condition")
        != "service_healthy"
    ):
        raise ValueError("Control must wait for the required RabbitMQ service")


if __name__ == "__main__":
    try:
        validate_deployment(json.load(sys.stdin))
    except (KeyError, TypeError, ValueError) as error:
        print(f"runtime-deployment=FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(
        "runtime-deployment=PASS (configuration only; Worker preflight remains required)"
    )
