"""Build one sealed result from a stopped task in a bounded Worker child."""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import signal
import socket
import stat
import struct
import sys
from contextlib import suppress
from typing import Any

from dlr.worker.claim_task_child_v4 import (
    _exact,
    _invalid_constant,
    _pairs,
    _receive,
    _receive_directory,
    _send,
)


def _read_file(directory: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 0 <= before.st_size <= limit
        ):
            raise ValueError("result file unavailable")
        data = bytearray()
        while len(data) < before.st_size:
            chunk = os.read(fd, min(65_536, before.st_size - len(data)))
            if not chunk:
                raise ValueError("result file unavailable")
            data.extend(chunk)
        after = os.fstat(fd)
        visible = os.stat(name, dir_fd=directory, follow_symlinks=False)

        def fingerprint(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if fingerprint(before) != fingerprint(after) or fingerprint(before) != fingerprint(visible):
            raise ValueError("result file changed")
        return bytes(data)
    finally:
        os.close(fd)


def _run(channel: socket.socket, address_space_bytes: int) -> None:
    from dlr.common.result_intent_mac_v4 import stream_result_request
    from dlr.control.schemas.reliable_runtime import AttemptResultBody
    from dlr.worker import sandbox

    request = _receive(channel)
    if set(request) != {
        "attempt_id",
        "fencing_token",
        "claim_token",
        "exit_code",
        "stop_reason",
        "stdout_bytes",
        "stdout_truncated",
        "output_max_bytes",
        "preview_max_bytes",
        "max_body_bytes",
        "workspace_device",
        "workspace_inode",
    }:
        raise ValueError("result unavailable")
    output_limit, preview_limit, max_body = (
        request["output_max_bytes"],
        request["preview_max_bytes"],
        request["max_body_bytes"],
    )
    stdout_size = request["stdout_bytes"]
    if (
        type(request["attempt_id"]) is not int
        or not 0 < request["attempt_id"] < 2**63
        or type(request["fencing_token"]) is not int
        or not 0 < request["fencing_token"] < 2**63
        or type(request["claim_token"]) is not str
        or not 1 <= len(request["claim_token"]) <= 512
        or type(stdout_size) is not int
        or not 0 <= stdout_size <= address_space_bytes
        or type(request["stdout_truncated"]) is not bool
        or type(output_limit) is not int
        or not 0 < output_limit <= address_space_bytes
        or type(preview_limit) is not int
        or not 0 < preview_limit <= output_limit
        or type(max_body) is not int
        or not 0 < max_body <= address_space_bytes
        or type(request["workspace_device"]) is not int
        or type(request["workspace_inode"]) is not int
    ):
        raise ValueError("result unavailable")
    stdout = _exact(channel, stdout_size).decode("utf-8", errors="replace")
    directory = _receive_directory(channel)
    result_fd = -1
    try:
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino) != (request["workspace_device"], request["workspace_inode"]):
            raise ValueError("result workspace unavailable")
        result: dict[str, Any] = {
            "attempt_id": request["attempt_id"],
            "fencing_token": request["fencing_token"],
            "claim_token": request["claim_token"],
            "stdout": stdout,
            "stdout_truncated": request["stdout_truncated"],
            "stderr": "",
            "stderr_truncated": False,
            "workspace_cleanup_status": "deferred",
            "workspace_cleanup_error_code": "workspace_cleanup_failed",
        }
        exit_code, stop_reason = request["exit_code"], request["stop_reason"]
        if stop_reason == "execution_cutoff" and exit_code is None:
            result.update(
                status="timed_out",
                error="execution timed out",
                error_code="execution_timeout",
                error_class="timeout",
            )
        elif type(exit_code) is int and 0 < exit_code < 128 and stop_reason is None:
            result.update(
                status="failed",
                error="adapter process failed",
                error_code="attempt_failed",
                error_class="business_error",
            )
        elif exit_code == 0 and type(exit_code) is int and stop_reason is None:
            try:
                output = _read_file(directory, "output.json", output_limit)
            except FileNotFoundError:
                result.update(
                    status="failed",
                    error="adapter produced no output.json",
                    error_code="attempt_failed",
                    error_class="business_error",
                )
            else:
                metadata = json.loads(
                    _read_file(directory, ".dlr-output-meta", 4096),
                    object_pairs_hook=_pairs,
                    parse_constant=_invalid_constant,
                )
                if (
                    type(metadata) is not dict
                    or set(metadata) != {"size", "truncated"}
                    or type(metadata["size"]) is not int
                    or metadata["size"] < 0
                    or type(metadata["truncated"]) is not bool
                    or metadata["truncated"] != (metadata["size"] > output_limit)
                    or len(output) != min(metadata["size"], output_limit)
                ):
                    raise ValueError("result output unavailable")
                if metadata["truncated"]:
                    result.update(
                        status="succeeded",
                        output_truncated=True,
                        output_size=metadata["size"],
                        output_preview=output[:preview_limit].decode("utf-8", errors="ignore"),
                        error_code="output_too_large",
                    )
                else:
                    try:
                        value = json.loads(
                            output, object_pairs_hook=_pairs, parse_constant=_invalid_constant
                        )
                    except (UnicodeError, ValueError, RecursionError):
                        result.update(
                            status="failed",
                            error="invalid output.json",
                            error_code="attempt_failed",
                            error_class="business_error",
                        )
                    else:
                        encoded = json.dumps(
                            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                        ).encode("utf-8")
                        if len(encoded) <= output_limit:
                            result.update(
                                status="succeeded", output=value, output_size=len(encoded)
                            )
                        else:
                            result.update(
                                status="succeeded",
                                output_truncated=True,
                                output_size=len(encoded),
                                output_preview=encoded[:preview_limit].decode(
                                    "utf-8", errors="ignore"
                                ),
                                error_code="output_too_large",
                            )
        else:
            raise ValueError("result stop unverified")
        action = AttemptResultBody.model_validate(result)
        body = {
            name: getattr(action, name)
            for name in AttemptResultBody.model_fields
            if name != "claim_token"
        }
        result_fd = os.memfd_create(  # type: ignore[attr-defined, unused-ignore]
            "dlr-result",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,  # type: ignore[attr-defined, unused-ignore]
        )

        def write(data: bytes) -> None:
            view = memoryview(data)
            while view:
                written = os.write(result_fd, view)
                if written <= 0:
                    raise OSError("result unavailable")
                view = view[written:]

        mac, size = stream_result_request(body, action.claim_token, max_body, write)
        seals = sandbox._launch_seals()
        fcntl.fcntl(result_fd, fcntl.F_ADD_SEALS, seals)  # type: ignore[attr-defined, unused-ignore]
        if fcntl.fcntl(result_fd, fcntl.F_GET_SEALS) & seals != seals:  # type: ignore[attr-defined, unused-ignore]
            raise ValueError("result snapshot unavailable")
        _send(channel, {"state": "result", "size_bytes": size, "body_mac": mac.hex()})
        if (
            channel.sendmsg(
                [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", result_fd))]
            )
            != 1
        ):
            raise ValueError("result snapshot unavailable")
    finally:
        if result_fd >= 0:
            os.close(result_fd)
        os.close(directory)


def main() -> int:
    try:
        if sys.platform != "linux" or len(sys.argv) != 5:
            raise ValueError("result process unavailable")
        fd, parent_pid, deadline_ns, address_space = (int(value) for value in sys.argv[1:])
        from dlr.worker.claim_http_child_v4 import _arm_deadline, _limit_address_space

        libc = ctypes.CDLL(None, use_errno=True)
        if (
            libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0
            or os.getppid() != parent_pid
            or libc.prctl(4, 0, 0, 0, 0) != 0
        ):
            raise ValueError("result process unavailable")
        _limit_address_space(address_space)
        _arm_deadline(deadline_ns)
        with socket.socket(fileno=fd) as channel:
            channel.sendall(b"R")
            _run(channel, address_space)
        return 0
    except BaseException:
        with suppress(Exception):
            socket.socket(fileno=int(sys.argv[1])).close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
