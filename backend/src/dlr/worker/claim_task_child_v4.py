"""Receive and validate one tentative Claim inside a bounded Worker process.

No Attempt is committed by this process and no business code is started. The
parent owns the Broker delivery, local journal, budget and final confirmation.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import urllib.request
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

_MAX_FRAME = 65_536


def _exact(channel: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = channel.recv(min(size - len(data), 65_536))
        if not part:
            raise ValueError("Claim parent unavailable")
        data.extend(part)
    return bytes(data)


def _receive(channel: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _exact(channel, 4))[0]
    if not 0 < size <= _MAX_FRAME:
        raise ValueError("Claim frame unavailable")
    value = json.loads(_exact(channel, size))
    if type(value) is not dict:
        raise ValueError("Claim frame unavailable")
    return value


def _send(channel: socket.socket, value: dict[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if not 0 < len(raw) <= _MAX_FRAME:
        raise ValueError("Claim frame unavailable")
    channel.sendall(struct.pack("!I", len(raw)) + raw)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid constant")


def _receive_fd(channel: socket.socket) -> int:
    """Receive exactly one close-on-exec descriptor, closing rejected rights."""
    descriptors: list[int] = []
    try:
        cloexec = getattr(socket, "MSG_CMSG_CLOEXEC", None)
        if not isinstance(cloexec, int) or isinstance(cloexec, bool):
            raise ValueError("workspace handoff unavailable")
        data, ancillary, flags, _ = channel.recvmsg(
            1, socket.CMSG_SPACE(struct.calcsize("i") * 4), cloexec
        )
        invalid = False
        for level, kind, raw in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                invalid = True
                continue
            size = struct.calcsize("i")
            invalid |= bool(len(raw) % size)
            descriptors.extend(
                value[0] for value in struct.iter_unpack("i", raw[: len(raw) // size * size])
            )
        if (
            invalid
            or data != b"D"
            or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)
            or len(descriptors) != 1
        ):
            raise ValueError("workspace handoff unavailable")
        fd = descriptors[0]
        descriptors.clear()
        return fd
    finally:
        for fd in descriptors:
            with suppress(OSError):
                os.close(fd)


def _receive_directory(channel: socket.socket) -> int:
    fd = _receive_fd(channel)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("workspace handoff unavailable")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _stage_python(
    channel: socket.socket, task: Any, fingerprint: str, command: dict[str, Any]
) -> None:
    """Stage a bounded Python/JavaScript snapshot while this child owns its payload."""
    from dlr.common.worker_languages_v4 import NODE_EXECUTABLE
    from dlr.runtime import harness, node_harness
    from dlr.worker import sandbox, task_fingerprint_v4

    if set(command) != {
        "op",
        "workspace",
        "workspace_device",
        "workspace_inode",
        "staging_bytes",
        "launch_bytes",
        "fingerprint",
    }:
        raise ValueError("task staging unavailable")
    path = command["workspace"]
    staging_bytes, launch_bytes = command["staging_bytes"], command["launch_bytes"]
    if (
        type(path) is not str
        or not Path(path).is_absolute()
        or Path(path).name != f"dlr-exec-{task.execution_id}"
        or Path(path).parent.name != f"attempt-{task.attempt_id}"
        or type(staging_bytes) is not int
        or not 0 < staging_bytes <= task.resource_profile.tmp_bytes
        or type(launch_bytes) is not int
        or not 0 < launch_bytes <= task.resource_profile.memory_bytes
        or command["fingerprint"] != fingerprint
        or task_fingerprint_v4.fingerprint(task, task.claim_token).hex() != fingerprint
    ):
        raise ValueError("task staging unavailable")
    directory = _receive_directory(channel)
    launch_fd = -1
    try:
        info = os.fstat(directory)
        visible = os.stat(path, follow_symlinks=False)
        if (
            type(command["workspace_device"]) is not int
            or type(command["workspace_inode"]) is not int
            or (info.st_dev, info.st_ino)
            != (command["workspace_device"], command["workspace_inode"])
            or (info.st_dev, info.st_ino) != (visible.st_dev, visible.st_ino)
        ):
            raise ValueError("task staging unavailable")
        for name in ("input", "temp", "output"):
            os.mkdir(name, mode=0o700, dir_fd=directory)
        remaining = staging_bytes

        def write(name: str, chunks: Any) -> None:
            nonlocal remaining
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            try:
                for chunk in chunks:
                    parts: Iterable[bytes]
                    if type(chunk) is str:
                        parts = (
                            chunk[offset : offset + 4096].encode("utf-8")
                            for offset in range(0, len(chunk), 4096)
                        )
                    elif type(chunk) is bytes:
                        parts = (chunk,)
                    else:
                        raise ValueError("task staging unavailable")
                    for part in parts:
                        if len(part) > remaining:
                            raise ValueError("task staging unavailable")
                        remaining -= len(part)
                        view = memoryview(part)
                        while view:
                            written = os.write(fd, view)
                            if written <= 0:
                                raise OSError("task staging unavailable")
                            view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)

        if task.language == "python":
            source = Path(harness.__file__).resolve(strict=True)
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                before = os.fstat(source_fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > remaining:
                    raise ValueError("task staging unavailable")
                write(".dlr-harness.py", iter(lambda: os.read(source_fd, 4096), b""))
                after = os.fstat(source_fd)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError("task staging unavailable")
            finally:
                os.close(source_fd)
            write("adapter.py", (task.code,))
            argv = [sys.executable, "-I", str(Path(path) / ".dlr-harness.py"), path]
        elif task.language == "javascript":
            write(".dlr-harness.mjs", (node_harness.SOURCE,))
            write("adapter.mjs", (task.code,))
            argv = [
                NODE_EXECUTABLE,
                str(Path(path) / ".dlr-harness.mjs"),
                path,
                str(Path(path) / "adapter.mjs"),
            ]
        else:
            raise ValueError("task staging unavailable")
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
        write("input.json", encoder.iterencode(task.input))
        write("runtime_config.json", encoder.iterencode(task.runtime_config))
        write(
            "input_manifest.json",
            encoder.iterencode({"execution_id": task.execution_id, "files": []}),
        )
        os.fsync(directory)
        if task_fingerprint_v4.fingerprint(task, task.claim_token).hex() != fingerprint:
            raise ValueError("task staging unavailable")
        environment = (
            {"PATH": "/usr/bin:/bin"}
            if task.language == "javascript"
            else {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        )
        from dlr.common import business_credentials_v4 as business_contract

        bound_values = business_contract.values(
            business_contract.bindings(task.credential_bindings), task.secrets
        )
        environment.update({f"DLR_SECRET_{key}": item for key, item in bound_values.items()})
        launch_fd = sandbox.create_sealed_launch(
            argv,
            environment,
            max_bytes=launch_bytes,
        )
        _send(
            channel, {"state": "staged", "fingerprint": fingerprint, "launch_bytes": launch_bytes}
        )
        if (
            channel.sendmsg(
                [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", launch_fd))]
            )
            != 1
        ):
            raise ValueError("launch handoff unavailable")
    finally:
        if launch_fd >= 0:
            os.close(launch_fd)
        os.close(directory)


def _prepare(channel: socket.socket) -> None:
    from dlr.common.dispatch_protocol import deserialize_dispatch_message
    from dlr.common.lease_proof_wire_v4 import verify_worker_proof_signature
    from dlr.common.worker_protocol_v4 import is_worker_credential, validate_control_url
    from dlr.control.schemas.reliable_runtime import V4TaskPayload
    from dlr.worker import task_fingerprint_v4
    from dlr.worker.client import _open_v4

    value = _receive(channel)
    expected = {
        "base_url",
        "worker_id",
        "worker_identity_id",
        "instance_id",
        "generation",
        "credential",
        "instance_proof",
        "verification_key",
        "cluster_id",
        "authority_epoch",
        "dispatch",
        "wire_budget",
        "timeout",
        "business_credentials",
    }
    if set(value) != expected:
        raise ValueError("Claim request unavailable")
    base_url = value["base_url"]
    validate_control_url(base_url)
    worker_id = value["worker_id"]
    identity = UUID(value["worker_identity_id"])
    instance = UUID(value["instance_id"])
    generation = value["generation"]
    credential = value["credential"]
    proof = value["instance_proof"]
    key = bytes.fromhex(value["verification_key"])
    cluster = UUID(value["cluster_id"])
    epoch = UUID(value["authority_epoch"])
    message = deserialize_dispatch_message(value["dispatch"])
    wire_budget, timeout = value["wire_budget"], value["timeout"]
    claims = verify_worker_proof_signature(proof, key)
    if (
        type(value["business_credentials"]) is not bool
        or type(worker_id) is not int
        or not 0 < worker_id < 2**63
        or type(generation) is not int
        or not 0 < generation < 2**63
        or not is_worker_credential(credential)
        or credential in base_url
        or claims is None
        or claims.cluster_id != cluster
        or claims.authority_epoch != epoch
        or claims.worker_id != worker_id
        or claims.worker_identity_id != identity
        or claims.instance_id != instance
        or claims.generation != generation
        or message.target_worker_id != worker_id
        or type(wire_budget) is not int
        or not 4096 <= wire_budget < 2**63
        or type(timeout) not in (int, float)
        or not 0 < timeout <= 5
    ):
        raise ValueError("Claim request unavailable")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/workers/{worker_id}/v4/claim/prepare",
        data=json.dumps(message.model_dump(mode="json"), separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {credential}",
            "X-DLR-Worker-Instance": str(instance),
            "X-DLR-Worker-Generation": str(generation),
            "X-DLR-Worker-Lease-Proof": proof,
            "X-DLR-Worker-Claim-Wire-Bytes": str(wire_budget),
        },
    )
    with _open_v4(request, timeout=float(timeout)) as response:
        lengths = response.headers.get_all("Content-Length", [])
        proposals = response.headers.get_all("X-DLR-Claim-Proposal", [])
        encodings = response.headers.get_all("Content-Encoding", [])
        if (
            response.status != 200
            or len(lengths) != 1
            or re.fullmatch(r"[1-9][0-9]{0,18}", lengths[0]) is None
            or int(lengths[0]) > wire_budget
            or encodings not in ([], ["identity"])
            or response.headers.get_all("X-DLR-Worker-Attempt-Proof", [])
            or len(proposals) > 1
        ):
            raise ValueError("Claim response unavailable")
        expected_size = int(lengths[0])
        buffer = io.BytesIO()
        digest = hashlib.sha256()
        received = 0
        while received < expected_size:
            part = response.read(min(65_536, expected_size - received))
            if not isinstance(part, bytes) or not part:
                raise ValueError("Claim response unavailable")
            received += len(part)
            digest.update(part)
            buffer.write(part)
        if response.read(1):
            raise ValueError("Claim response unavailable")
    document = json.loads(
        buffer.getvalue(), object_pairs_hook=_pairs, parse_constant=_invalid_constant
    )
    if type(document) is not dict or set(document) != {
        "decision",
        "reason",
        "retry_after_seconds",
        "attempt_id",
        "cancel_requested",
        "payload",
    }:
        raise ValueError("Claim response unavailable")
    decision, reason = document["decision"], document["reason"]
    if type(reason) is not str or not 1 <= len(reason) <= 64:
        raise ValueError("Claim response unavailable")
    if decision != "EXECUTE":
        retry = document["retry_after_seconds"]
        if (
            decision not in {"ACK_NOOP", "DEFER", "REJECT_DLQ", "PAUSE_CONSUMER"}
            or proposals
            or document["payload"] is not None
            or document["attempt_id"] is not None
            or document["cancel_requested"] is not False
            or (
                decision in {"DEFER", "PAUSE_CONSUMER"}
                and (type(retry) is not int or not 0 <= retry <= 86_400)
            )
            or (decision not in {"DEFER", "PAUSE_CONSUMER"} and retry is not None)
        ):
            raise ValueError("Claim response unavailable")
        _send(
            channel,
            {
                "state": "decision",
                "decision": decision,
                "reason": reason,
                "retry_after_seconds": retry,
            },
        )
        return
    if (
        len(proposals) != 1
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", proposals[0]) is None
        or type(document["cancel_requested"]) is not bool
        or document["cancel_requested"]
        or document["retry_after_seconds"] is not None
    ):
        raise ValueError("Claim response unavailable")
    task = V4TaskPayload.model_validate(document["payload"])
    if (
        type(document["attempt_id"]) is not int
        or task.attempt_id != document["attempt_id"]
        or task.worker_identity_id != identity
        or task.instance_id != instance
        or task.instance_generation != generation
        or task.execution_id != message.execution_id
        or task.adapter_id != message.adapter_id
        or task.language != message.language
        or task.resource_profile.resource_class != message.resource_class
        or task.execution_timeout_seconds != task.resource_profile.execution_timeout_seconds
        or task.recovery_grace_seconds_snapshot != task.resource_profile.recovery_grace_seconds
        or task.workspace_cleanup_attempt_timeout_seconds_snapshot
        != task.resource_profile.workspace_cleanup_attempt_timeout_seconds
        or task.workspace_cleanup_total_timeout_seconds_snapshot
        != task.resource_profile.workspace_cleanup_total_timeout_seconds
    ):
        raise ValueError("Claim response unavailable")
    if (
        task.language not in {"python", "javascript"}
        or task.requirements
        or task.input_files
        or task.builtin_package_snapshot is not None
        or task.dependency_check
        or ((task.secrets or task.credential_bindings) and not value["business_credentials"])
    ):
        _send(
            channel,
            {
                "state": "decision",
                "decision": "PAUSE_CONSUMER",
                "reason": "worker_task_capability_unavailable",
                "retry_after_seconds": 30,
            },
        )
        return
    from dlr.common import business_credentials_v4 as business_contract

    business_contract.values(business_contract.bindings(task.credential_bindings), task.secrets)
    task_fingerprint = task_fingerprint_v4.fingerprint(task, task.claim_token).hex()
    del buffer, document, request, value, credential, proof
    # The business snapshot remains here; the Agent receives only bounded
    # ownership, resource and token metadata needed to journal and confirm.
    _send(
        channel,
        {
            "state": "prepared",
            "proposal": proposals[0],
            "wire_sha256": digest.hexdigest(),
            "task_fingerprint": task_fingerprint,
            "execution_id": task.execution_id,
            "attempt_id": task.attempt_id,
            "attempt_no": task.attempt_no,
            "fencing_token": task.fencing_token,
            "renew_seconds": task.renew_seconds,
            "lease_expires_at": task.lease_expires_at.isoformat(),
            "claim_token": task.claim_token,
            "cleanup_token": task.cleanup_token,
            "resource_profile": task.resource_profile.model_dump(mode="json"),
            "business_credentials": bool(task.credential_bindings),
        },
    )
    command = _receive(channel)
    if command.get("op") == "stage":
        _stage_python(channel, task, task_fingerprint, command)
        command = _receive(channel)
    if command != {"op": "stop"}:
        raise ValueError("Claim handoff unavailable")


def main() -> int:
    try:
        if sys.platform != "linux" or len(sys.argv) != 5:
            raise ValueError
        fd, parent_pid, deadline_ns, address_space_bytes = (int(value) for value in sys.argv[1:])
        from dlr.worker.claim_http_child_v4 import _arm_deadline, _limit_address_space

        libc = ctypes.CDLL(None, use_errno=True)
        if (
            libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0
            or os.getppid() != parent_pid
            or libc.prctl(4, 0, 0, 0, 0) != 0
        ):
            raise ValueError("Claim process unavailable")
        _limit_address_space(address_space_bytes)
        _arm_deadline(deadline_ns)
        with socket.socket(fileno=fd) as channel:
            channel.sendall(b"R")
            _prepare(channel)
        return 0
    except BaseException:
        # Never send exceptions, payloads, tokens or response bodies to logs.
        with suppress(Exception):
            socket.socket(fileno=int(sys.argv[1])).close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
