"""Bounded file staging inside the existing, nonbusiness Claim child.

The parent sends current proofs only through admission, then releases its
renewal locks before this process reads a body. No file bytes cross that IPC.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import stat
import urllib.error
import urllib.request
import zipfile
from contextlib import suppress
from typing import Any

from dlr.common import builtin_wheel_v4 as wheels
from dlr.common import managed_files_v4 as contract
from dlr.common.worker_protocol_v4 import validate_control_url
from dlr.worker.claim_http_v4 import _clock
from dlr.worker.claim_task_child_v4 import _receive, _send
from dlr.worker.client import _open_v4
from dlr.worker.workspace import MOUNT_NAME_PATTERN

PREFLIGHT_CODE = (
    "managed_probe=False\n"
    "probe=pathlib.Path('managed-input-probe'); probe.mkdir(mode=0o700)\n"
    "leaf=probe/'input-00.txt'; expected=b'dlr-managed-input-probe\\0'*2048\n"
    "with leaf.open('xb') as stream:\n"
    "    stream.write(expected); stream.flush(); os.fsync(stream.fileno())\n"
    "leaf.chmod(0o444); probe.chmod(0o555)\n"
    "with leaf.open('rb') as stream:\n"
    "    assert stream.read(len(expected)+1)==expected\n"
    "probe.chmod(0o700); leaf.unlink(); probe.rmdir()\n"
    "assert not probe.exists()\n"
    "managed_probe=True\n"
)


class FilePreparationFailed(Exception):
    """Known file failure after this process closed all its I/O owners."""


class FileIOUnknown(Exception):
    """An uncertain close must not produce a normal preparation receipt."""


def _close(stream: Any) -> None:
    try:
        stream.close()
    except BaseException:
        raise FileIOUnknown("input I/O unavailable") from None


def _remaining(deadline: float) -> float:
    remaining = deadline - _clock()
    if not math.isfinite(remaining) or remaining <= 0:
        raise FilePreparationFailed("input preparation failed")
    return remaining


def receipt(value: Any) -> list[dict[str, Any]]:
    """Small path-free records, never original names or body bytes."""
    if type(value) is not list or len(value) > contract.MAX_FILES:
        raise ValueError("input receipt unavailable")
    total = 0
    for ordinal, item in enumerate(value):
        if (
            type(item) is not dict
            or set(item) != {"mount_name", "size_bytes", "sha256", "device", "inode"}
            or type(item["mount_name"]) is not str
            or not _staged_name(item["mount_name"])
            or not item["mount_name"].startswith(f"input-{ordinal:02d}.")
            or type(item["size_bytes"]) is not int
            or not 0 <= item["size_bytes"] <= contract.MAX_FILE_BYTES
            or type(item["sha256"]) is not str
            or len(item["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in item["sha256"])
            or any(
                type(item[k]) is not int or not 0 <= item[k] < 2**63 for k in ("device", "inode")
            )
        ):
            raise ValueError("input receipt unavailable")
        if item["mount_name"] == wheels.MOUNT_NAME and (len(value) != 1 or item["size_bytes"] <= 0):
            raise ValueError("builtin wheel receipt unavailable")
        total += item["size_bytes"]
    if total > contract.MAX_TOTAL_BYTES:
        raise ValueError("input receipt unavailable")
    return value


def _staged_name(name: str) -> bool:
    # Only the builtin transfer has this fixed name. The ordinary upload and
    # managed-file descriptor allowlists remain unchanged.
    return bool(name == wheels.MOUNT_NAME or MOUNT_NAME_PATTERN.fullmatch(name))


def discard_after_reap(directory: int, execution_id: int) -> None:
    """Remove only bounded staging objects after their sole creator was reaped.

    This function does not establish that premise. Its caller owns the exact
    pidfd, joins the original creator and separately proves the empty cgroup.
    No contents are read or hashed in the parent.
    """
    for name in ("input.part", "input"):
        try:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory
            )
        except FileNotFoundError:
            continue
        try:
            info = os.fstat(child)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in {0o700, 0o555}:
                raise FileIOUnknown("input cleanup unavailable")
            entries = list(os.scandir(child))
            if len(entries) > contract.MAX_FILES:
                raise FileIOUnknown("input cleanup unavailable")
            total = 0
            checked = []
            for entry in entries:
                item = entry.stat(follow_symlinks=False)
                if (
                    not _staged_name(entry.name)
                    or not stat.S_ISREG(item.st_mode)
                    or item.st_uid != os.geteuid()
                    or item.st_nlink != 1
                    or stat.S_IMODE(item.st_mode) not in {0o600, 0o444}
                    or not 0 <= item.st_size <= contract.MAX_FILE_BYTES
                ):
                    raise FileIOUnknown("input cleanup unavailable")
                total += item.st_size
                checked.append((entry.name, item.st_dev, item.st_ino))
            if total > contract.MAX_TOTAL_BYTES:
                raise FileIOUnknown("input cleanup unavailable")
            os.fchmod(child, 0o700)
            for filename, device, inode in checked:
                item = os.stat(filename, dir_fd=child, follow_symlinks=False)
                if (item.st_dev, item.st_ino) != (device, inode):
                    raise FileIOUnknown("input cleanup unavailable")
                os.unlink(filename, dir_fd=child)
            if name == "input.part":
                os.rmdir(name, dir_fd=directory)
        finally:
            os.close(child)
    with suppress(FileExistsError):
        os.mkdir("input", mode=0o700, dir_fd=directory)
    # The only potentially interrupted metadata publication is deterministic
    # and contains no business body, secret, filename or authorization token.
    for name in ("input_manifest.part", "input_manifest.json"):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16_384
        ):
            raise FileIOUnknown("input manifest unavailable")
        os.unlink(name, dir_fd=directory)
    fd = os.open(
        "input_manifest.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        raw = json.dumps({"execution_id": execution_id, "files": []}).encode("ascii")
        if os.write(fd, raw) != len(raw):
            raise FileIOUnknown("input manifest unavailable")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)


def _fetch(
    channel: socket.socket,
    task: Any,
    descriptor: contract.InputFile,
    target: int,
    cutoff: float,
    *,
    builtin_package: bool = False,
) -> None:
    _send(channel, {"state": "input_authorization", "ordinal": descriptor.ordinal})
    auth = _receive(channel)
    if set(auth) != {
        "base_url",
        "worker_id",
        "instance_id",
        "generation",
        "credential",
        "instance_proof",
        "attempt_proof",
        "claim_token",
        "deadline",
    }:
        raise FileIOUnknown("input authorization unavailable")
    validate_control_url(auth["base_url"])
    deadline = auth["deadline"]
    if (
        type(deadline) is not float
        or not math.isfinite(deadline)
        or deadline > cutoff
        or auth["claim_token"] != task.claim_token
        or auth["instance_id"] != str(task.instance_id)
        or auth["generation"] != task.instance_generation
        or type(auth["worker_id"]) is not int
        or not 0 < auth["worker_id"] < 2**63
    ):
        raise FileIOUnknown("input authorization unavailable")
    deadline = min(deadline, _clock() + contract.FILE_TIMEOUT_SECONDS)
    material = "builtin-packages" if builtin_package else "input-artifacts"
    request = urllib.request.Request(
        f"{auth['base_url'].rstrip('/')}/api/workers/{auth['worker_id']}/v4/"
        f"executions/{task.execution_id}/{material}/{descriptor.artifact_id}/content",
        method="GET",
        headers={
            "Authorization": f"Bearer {auth['credential']}",
            "Accept-Encoding": "identity",
            "X-DLR-Worker-Instance": auth["instance_id"],
            "X-DLR-Worker-Generation": str(auth["generation"]),
            "X-DLR-Worker-Lease-Proof": auth["instance_proof"],
            "X-DLR-Worker-Attempt-Proof": auth["attempt_proof"],
            "X-DLR-Claim-Token": auth["claim_token"],
        },
    )
    stream = None
    admitted = False
    try:
        try:
            stream = _open_v4(request, timeout=_remaining(deadline))
        except urllib.error.HTTPError as error:
            _close(error)
            raise FilePreparationFailed("input preparation failed") from None
        lengths = stream.headers.get_all("Content-Length", [])
        if (
            stream.status != 200
            or lengths != [str(descriptor.size_bytes)]
            or stream.headers.get_all("Content-Encoding", []) not in ([], ["identity"])
            or stream.headers.get_all("Transfer-Encoding", [])
            or stream.headers.get_all("Content-Range", [])
            or stream.geturl() != request.full_url
        ):
            raise FilePreparationFailed("input preparation failed")
        _remaining(deadline)
        _send(channel, {"state": "input_admitted", "ordinal": descriptor.ordinal})
        admitted = True
        # The parent acknowledges only after releasing both renewal locks.
        if _receive(channel) != {"op": "read_input"}:
            raise FileIOUnknown("input authority unavailable")
        del auth, request
        digest = hashlib.sha256()
        total = 0
        while True:
            _remaining(deadline)
            # urllib's initial timeout applies to individual operations;
            # shrink the real socket timeout before every bounded read.
            fp = getattr(stream, "fp", None)
            if fp is not None:
                sock = getattr(getattr(fp, "raw", None), "_sock", None)
                if not isinstance(sock, socket.socket):
                    raise FileIOUnknown("input socket unavailable")
                sock.settimeout(_remaining(deadline))
            part = stream.read(min(contract.CHUNK_BYTES, descriptor.size_bytes - total + 1))
            _remaining(deadline)
            if not isinstance(part, bytes) or total + len(part) > descriptor.size_bytes:
                raise FilePreparationFailed("input preparation failed")
            if not part:
                break
            total += len(part)
            digest.update(part)
            view = memoryview(part)
            while view:
                _remaining(deadline)
                count = os.write(target, view)
                if count <= 0:
                    raise OSError("input preparation failed")
                view = view[count:]
            del part, view
        if total != descriptor.size_bytes or digest.hexdigest() != descriptor.sha256:
            raise FilePreparationFailed("input preparation failed")
    except FileIOUnknown:
        raise
    except Exception:
        if stream is not None:
            closing, stream = stream, None
            _close(closing)
        if not admitted:
            _send(channel, {"state": "input_rejected", "ordinal": descriptor.ordinal})
            if _receive(channel) != {"op": "read_input"}:
                raise FileIOUnknown("input authority unavailable") from None
        raise FilePreparationFailed("input preparation failed") from None
    finally:
        if stream is not None:
            closing, stream = stream, None
            _close(closing)


def stage(
    channel: socket.socket,
    task: Any,
    directory: int,
    *,
    budget: int,
    deadline: float,
    wheel: contract.InputFile | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Publish only the whole verified set; failures leave the original empty input."""
    metadata = [item.model_dump(mode="json") for item in task.input_files]
    files = contract.descriptors(metadata) if wheel is None else (wheel,)
    total = sum(item.size_bytes for item in files)
    # Original staging plus the sandbox's independent copy can coexist.
    manifest = [
        {"artifact_id": item.artifact_id, **{k: v for k, v in original.items() if k != "id"}}
        for item, original in zip(files if wheel is None else (), metadata, strict=True)
    ]
    manifest_bytes = json.dumps(
        {"execution_id": task.execution_id, "files": manifest},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    expanded = wheels.MAX_EXPANDED_BYTES if wheel is not None else 0
    if len(manifest_bytes) > 16_384 or 2 * (total + len(manifest_bytes)) + expanded > budget:
        raise FilePreparationFailed("input preparation failed")
    os.mkdir("input.part", mode=0o700, dir_fd=directory)
    part = os.open(
        "input.part",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory,
    )
    created: list[str] = []
    records: list[dict[str, Any]] = []
    published = False
    manifest_created = False
    try:
        for item in files:
            _remaining(deadline)
            fd = os.open(
                item.mount_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=part,
            )
            created.append(item.mount_name)
            try:
                _fetch(channel, task, item, fd, deadline, builtin_package=wheel is not None)
                if wheel is not None:
                    try:
                        with (
                            os.fdopen(fd, "rb", closefd=False) as stream,
                            zipfile.ZipFile(stream) as archive,
                        ):
                            wheels.members(archive)
                        _remaining(deadline)
                    except (OSError, ValueError, zipfile.BadZipFile):
                        raise FilePreparationFailed("builtin wheel unavailable") from None
                os.fsync(fd)
                os.fchmod(fd, 0o444)
                info = os.fstat(fd)
                visible = os.stat(item.mount_name, dir_fd=part, follow_symlinks=False)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size != item.size_bytes
                    or (info.st_dev, info.st_ino) != (visible.st_dev, visible.st_ino)
                ):
                    raise FileIOUnknown("input object unavailable")
                records.append(
                    {
                        "mount_name": item.mount_name,
                        "size_bytes": item.size_bytes,
                        "sha256": item.sha256,
                        "device": info.st_dev,
                        "inode": info.st_ino,
                    }
                )
            finally:
                try:
                    os.close(fd)
                except BaseException:
                    raise FileIOUnknown("input writer unavailable") from None
        os.fsync(part)
        manifest_fd = os.open(
            "input_manifest.part",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        manifest_created = True
        try:
            view = memoryview(manifest_bytes)
            while view:
                _remaining(deadline)
                written = os.write(manifest_fd, view)
                if written <= 0:
                    raise OSError("input manifest unavailable")
                view = view[written:]
            os.fsync(manifest_fd)
        finally:
            try:
                os.close(manifest_fd)
            except BaseException:
                raise FileIOUnknown("input manifest close unavailable") from None
        os.fchmod(part, 0o555)
        _remaining(deadline)
        os.rmdir("input", dir_fd=directory)
        os.rename("input.part", "input", src_dir_fd=directory, dst_dir_fd=directory)
        published = True
        # All bytes and metadata are verified and synced before publication.
        # The manifest is the last commit marker; business remains gated.
        os.rename(
            "input_manifest.part", "input_manifest.json", src_dir_fd=directory, dst_dir_fd=directory
        )
        os.fsync(directory)
        return records, manifest, total
    except FileIOUnknown:
        raise
    except Exception:
        if published:
            raise FileIOUnknown("input publication unavailable") from None
        # This is solely the trusted child's unpublished, bounded set. No
        # business, other child or exported descriptor can be using it.
        os.fchmod(part, 0o700)
        for name in created:
            os.unlink(name, dir_fd=part)
        os.rmdir("input.part", dir_fd=directory)
        if manifest_created:
            os.unlink("input_manifest.part", dir_fd=directory)
        raise FilePreparationFailed("input preparation failed") from None
    finally:
        try:
            os.close(part)
        except BaseException:
            raise FileIOUnknown("input directory unavailable") from None
