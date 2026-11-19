"""Observe the preflighted cgroup and sign capacity outside task code."""

import hashlib
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from dlr.common.cpu_accounting import CPU_PERIOD_US, attempt_cpu_units, cpu_units
from dlr.common.jcs import canonicalize
from dlr.common.worker_capacity_report import (
    CapacityObservation,
    Resources,
    SignedCapacityObservation,
    sign_observation,
)
from dlr.worker import sandbox


def read_signing_key(path: Path) -> bytes:
    from dlr.control.services.control_declaration_v4 import read_private_json

    if path.resolve(strict=True) != path:
        raise ValueError("Worker verifier key unavailable")
    data = read_private_json(str(path))
    if type(data) is not dict or set(data) != {"schema_version", "host_capacity_signing_key"}:
        raise ValueError("Worker verifier key unavailable")
    key = data["host_capacity_signing_key"]
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or (type(key) is not str or re.fullmatch(r"[0-9a-f]{64}", key) is None)
    ):
        raise ValueError("Worker verifier key unavailable")
    return bytes.fromhex(key)


def signed_sample(
    *,
    identity: UUID,
    instance: UUID,
    generation: int,
    config: sandbox.SandboxConfig,
    receive_limits: sandbox.ResourceLimits,
    runtime_root: Path,
    journal_root: Path,
    cache_root: Path | None,
    key_file: Path,
) -> SignedCapacityObservation:
    envelope = sandbox.read_verified_resource_envelope(config)
    reserve = sandbox.agent_resource_headroom(config)
    quantum = CPU_PERIOD_US // 1000
    total = Resources(
        cpu_millis=cpu_units(envelope.cpu_cores, round_up=False) // quantum,
        memory_bytes=envelope.memory_bytes,
        pids=envelope.pids,
        tmp_bytes=envelope.tmp_bytes,
        slots=1,
    )
    headroom = Resources(
        cpu_millis=(
            cpu_units(reserve.cpu_cores, round_up=True)
            + attempt_cpu_units(receive_limits.cpu_cores)
            + quantum
            - 1
        )
        // quantum,
        memory_bytes=reserve.memory_bytes + receive_limits.memory_bytes,
        pids=reserve.pids + receive_limits.pids,
        tmp_bytes=reserve.tmp_bytes + receive_limits.tmp_bytes,
        slots=0,
    )
    roots = (runtime_root, journal_root) + ((cache_root,) if cache_root is not None else ())
    storage_ready = all(
        shutil.disk_usage(root).free >= headroom.tmp_bytes + config.tmp_bytes for root in roots
    )
    fingerprint = hashlib.sha256(
        canonicalize(
            {
                "total": {k: str(v) for k, v in total.model_dump().items()},
                "headroom": {k: str(v) for k, v in headroom.model_dump().items()},
                "source": envelope.source,
                "nofile": str(config.nofile),
            }
        )
    ).hexdigest()
    return sign_observation(
        CapacityObservation(
            worker_identity_id=identity,
            instance_id=instance,
            instance_generation=generation,
            sampled_at=datetime.now(UTC),
            envelope_fingerprint=fingerprint,
            total=total,
            headroom=headroom,
            nofile=config.nofile,
            storage_ready=storage_ready,
        ),
        read_signing_key(key_file),
    )
