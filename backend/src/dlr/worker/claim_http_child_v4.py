"""Stdlib-only isolated Worker HTTP IO child. Never run business code here."""

import base64
import binascii
import ctypes
import fcntl
import hashlib
import json
import os
import re
import resource
import signal
import socket
import ssl
import stat
import struct
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

# This stdlib-only child cannot import the parent package under ``-I -S``.
# Keep the wire ceiling aligned with ``common.material_transfer_v4``.
_MATERIAL_SEGMENT_BYTES = 256 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
        return parsed.int != 0 and str(parsed) == value
    except ValueError:
        return False


def _canonical_result_proof(value: object) -> bool:
    raw_length = struct.calcsize(">B16s16sQ16s16sQQQQ16sQ16s16s16s32sqq") + 64
    return _canonical_proof(value, raw_length)


def _canonical_cleanup_proof(value: object) -> bool:
    raw_length = struct.calcsize(">B16s16sQ16sQQ16sQ16sQQQ16s16s16sqq") + 64
    return _canonical_proof(value, raw_length)


def _canonical_proof(value: object, raw_length: int) -> bool:
    if type(value) is not str or len(value) != ((raw_length + 2) // 3) * 4:
        return False
    try:
        encoded = value.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        return len(raw) == raw_length and base64.urlsafe_b64encode(raw) == encoded
    except (binascii.Error, UnicodeError, ValueError):
        return False


def _read_exact(sock: socket.socket, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        part = sock.recv(min(65536, size - len(parts)))
        if not part:
            raise ValueError("control closed")
        parts.extend(part)
    return bytes(parts)


def _receive_output_fd(
    sock: socket.socket, *, sealed_size: int | None = None, initial_size: int = 0
) -> int:
    """Receive exactly one descriptor; close every unexpected descriptor."""
    cloexec = getattr(socket, "MSG_CMSG_CLOEXEC", None)
    if not isinstance(cloexec, int) or isinstance(cloexec, bool):
        raise ValueError("descriptor transfer unavailable")
    data, ancillary, flags, _ = sock.recvmsg(
        1, socket.CMSG_SPACE(struct.calcsize("i") * 4), cloexec
    )
    fds: list[int] = []
    try:
        invalid = False
        for level, kind, raw in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                invalid = True
                continue
            size = struct.calcsize("i")
            invalid |= bool(len(raw) % size)
            fds.extend(
                value[0] for value in struct.iter_unpack("i", raw[: len(raw) // size * size])
            )
        if (
            invalid
            or data != b"D"
            or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)
            or len(fds) != 1
        ):
            raise ValueError("invalid output descriptor")
        fd = fds[0]
        info = os.fstat(fd)
        status = fcntl.fcntl(fd, fcntl.F_GETFL)
        if sealed_size is None:
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != initial_size
                or status & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR)
                or status & os.O_APPEND
                or os.lseek(fd, 0, os.SEEK_CUR) != initial_size
            ):
                raise ValueError("invalid output file")
        else:
            seals = 0
            for name in ("F_SEAL_WRITE", "F_SEAL_GROW", "F_SEAL_SHRINK", "F_SEAL_SEAL"):
                value = getattr(fcntl, name, None)
                if type(value) is not int:
                    raise ValueError("sealed request unavailable")
                seals |= value
            get_seals = fcntl.F_GET_SEALS  # type: ignore[attr-defined, unused-ignore]
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 0
                or info.st_size != sealed_size
                or status & os.O_ACCMODE not in (os.O_RDONLY, os.O_RDWR)
                or status & os.O_APPEND
                or fcntl.fcntl(fd, get_seals) & seals != seals
            ):
                raise ValueError("invalid sealed request")
        fds.pop()
        return fd
    finally:
        for fd in fds:
            with suppress(OSError):
                os.close(fd)


class _PreadBody:
    """Read the sealed body without sharing a mutable file offset with the Agent."""

    def __init__(self, fd: int, size: int) -> None:
        self._fd, self._size, self._offset = fd, size, 0

    def read(self, size: int = -1) -> bytes:
        if size == -1:
            size = 65536
        if size == 0:
            return b""
        if type(size) is not int or size < 0:
            raise ValueError("invalid request read")
        if self._offset == self._size:
            return b""
        data = os.pread(self._fd, min(size, 65536, self._size - self._offset), self._offset)
        if not data:
            raise ValueError("short sealed request")
        self._offset += len(data)
        return data


def _download(response: Any, fd: int, *, size_bytes: int, sha256: str) -> None:
    """Stream exactly the frozen size and verify its digest before success."""
    lengths = response.headers.get_all("Content-Length", [])
    encodings = response.headers.get_all("Content-Encoding", [])
    if lengths != [str(size_bytes)] or (encodings and encodings != ["identity"]):
        raise ValueError("material response invalid")
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = response.read(min(65536, size_bytes - total + 1))
        if not isinstance(chunk, bytes):
            raise ValueError("material response invalid")
        if not chunk:
            break
        total += len(chunk)
        if total > size_bytes:
            raise ValueError("material response invalid")
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("material output unavailable")
            view = view[written:]
    if total != size_bytes or digest.hexdigest() != sha256:
        raise ValueError("material response invalid")


def _download_segment(
    response: Any, fd: int, *, size_bytes: int, sha256: str, offset: int, segment_bytes: int
) -> None:
    """Write one exactly framed segment; the caller verifies the complete file."""
    end = offset + segment_bytes - 1
    if (
        response.headers.get_all("Content-Length", []) != [str(segment_bytes)]
        or response.headers.get_all("Content-Range", []) != [f"bytes {offset}-{end}/{size_bytes}"]
        or response.headers.get_all("X-DLR-Experimental-Material-SHA256", []) != [sha256]
        or response.headers.get_all("Content-Encoding", []) not in ([], ["identity"])
    ):
        raise ValueError("material response invalid")
    total = 0
    while True:
        data = response.read(min(65536, segment_bytes - total + 1))
        if not isinstance(data, bytes):
            raise ValueError("material response invalid")
        if not data:
            break
        total += len(data)
        if total > segment_bytes:
            raise ValueError("material response invalid")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("material output unavailable")
            view = view[written:]
    if total != segment_bytes:
        raise ValueError("material response invalid")


def _verify_material_file(fd: int, *, size_bytes: int, sha256: str) -> None:
    """Hash the actual retained file, rejecting any observed identity change."""

    def fingerprint() -> tuple[int, ...]:
        info = os.fstat(fd)
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    before = fingerprint()
    if before[5] != size_bytes or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
        raise ValueError("material file unavailable")
    digest = hashlib.sha256()
    offset = 0
    while offset < size_bytes:
        data = os.pread(fd, min(65536, size_bytes - offset), offset)
        if not data:
            raise ValueError("material file incomplete")
        offset += len(data)
        digest.update(data)
    if fingerprint() != before or digest.hexdigest() != sha256:
        raise ValueError("material file invalid")


def _reset_material_file(fd: int, *, size_bytes: int, reset_to: int) -> None:
    """Discard a segment only on the exact already-open file."""
    before = os.fstat(fd)
    if before.st_size != size_bytes or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDWR:
        raise ValueError("material file unavailable")
    os.ftruncate(fd, reset_to)
    if os.lseek(fd, reset_to, os.SEEK_SET) != reset_to:
        raise ValueError("material file unavailable")
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_nlink) != (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_nlink,
    ) or after.st_size != reset_to:
        raise ValueError("material file changed")


def _read_response(response: Any, limit: int | None) -> bytes | bytearray:
    """Stop at a wire ceiling without interpreting Claim commitment."""
    if limit is None:
        body = response.read()
        if not isinstance(body, bytes):
            raise ValueError("invalid response stream")
        return body
    if type(limit) is not int or not 0 < limit < 2**63:
        raise ValueError("invalid response ceiling")
    body = bytearray()
    while True:
        size = min(65536, limit + 1 - len(body))
        part = response.read(size)
        if not isinstance(part, bytes) or len(part) > size:
            raise ValueError("invalid response stream")
        if not part:
            # The child sends this buffer directly over its private socket;
            # avoid copying the whole response just before that transfer.
            return body
        body.extend(part)
        if len(body) > limit:
            raise ValueError("response exceeds wire ceiling")


def _require_wire_ack(response: Any, limit: int | None) -> None:
    """Reject an unconfirmed probe, without claiming cluster compatibility."""
    if limit is not None:
        values = response.headers.get_all("X-DLR-Worker-Claim-Wire-Bytes", [])
        if values != [str(limit)]:
            raise ValueError("wire ceiling was not confirmed")


def _read_attempt_proof(response: Any, text_length: int, raw_length: int) -> bytes:
    """Transfer a single bounded opaque proof without trusting its contents."""
    if (
        type(text_length) is not int
        or type(raw_length) is not int
        or not 0 < raw_length <= 768
        or not 0 < text_length <= 1024
        or text_length != ((raw_length + 2) // 3) * 4
    ):
        raise ValueError("invalid proof format")
    values = response.headers.get_all("X-DLR-Worker-Attempt-Proof", [])
    if not values:
        return b""
    if len(values) != 1 or len(values[0]) != text_length:
        raise ValueError("invalid proof header")
    try:
        encoded: bytes = values[0].encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != raw_length or base64.urlsafe_b64encode(raw) != encoded:
            raise ValueError
        return encoded
    except (binascii.Error, TypeError, ValueError, UnicodeError):
        raise ValueError("invalid proof header") from None


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _Itimerspec(ctypes.Structure):
    _fields_ = [("it_interval", _Timespec), ("it_value", _Timespec)]


def _arm_deadline(deadline_ns: object) -> None:
    """Kernel kills this process at the suspend-aware absolute deadline."""
    if type(deadline_ns) is not int or deadline_ns <= 0:
        raise ValueError("invalid deadline")
    boot_clock = time.CLOCK_BOOTTIME  # type: ignore[attr-defined, unused-ignore]
    if time.clock_gettime_ns(boot_clock) >= deadline_ns:
        raise TimeoutError
    # The default SIGALRM disposition terminates even if the parent or this
    # process is stalled in Python/native IO. No Python signal handler runs.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    timer = ctypes.c_void_p()
    libc.timer_create.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    libc.timer_settime.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(_Itimerspec),
        ctypes.c_void_p,
    )
    spec = _Itimerspec(
        _Timespec(0, 0), _Timespec(deadline_ns // 1_000_000_000, deadline_ns % 1_000_000_000)
    )
    if libc.timer_create(boot_clock, None, ctypes.byref(timer)) != 0:
        raise OSError("timer unavailable")
    if libc.timer_settime(timer, 1, ctypes.byref(spec), None) != 0:
        raise OSError("timer unavailable")
    if time.clock_gettime_ns(boot_clock) >= deadline_ns:
        raise TimeoutError


def _limit_address_space(value: int) -> None:
    """Limit this child only; address space is not the cgroup memory charge."""
    if type(value) is not int or not 0 < value < 2**63 or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("invalid child address-space limit")
    resource.setrlimit(resource.RLIMIT_AS, (value, value))
    if resource.getrlimit(resource.RLIMIT_AS) != (value, value):
        raise ValueError("child address-space limit unavailable")


def main() -> int:
    # The parent task must still be the one that created this child after the
    # kernel installs the death signal. No network IO precedes this check.
    if not sys.platform.startswith("linux") or len(sys.argv) not in (4, 5):
        return 1
    fd, expected_parent, deadline_ns = map(int, sys.argv[1:4])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != expected_parent:
        return 1
    _arm_deadline(deadline_ns)
    if len(sys.argv) == 5:
        _limit_address_space(int(sys.argv[4]))
    with socket.socket(fileno=fd) as control:
        control.sendall(b"R")
        length = struct.unpack("!Q", _read_exact(control, 8))[0]
        data = json.loads(_read_exact(control, length))
        test_plaintext = data.get("test_only_plaintext_loopback", False)
        parts = urlsplit(data["url"])
        if type(test_plaintext) is not bool or (
            parts.scheme != "https"
            and not (
                test_plaintext and parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"}
            )
        ):
            raise ValueError("v4 TLS required")
        response_limit = data.get("response_limit_bytes")
        if response_limit is not None and (
            type(response_limit) is not int or not 0 < response_limit < 2**63
        ):
            raise ValueError("invalid response ceiling")
        proof_request = data.get("proof_request", False)
        if type(proof_request) is not bool:
            raise ValueError("invalid request mode")
        collect_timing = data.get("collect_request_timing", False)
        if type(collect_timing) is not bool or (collect_timing and not proof_request):
            raise ValueError("invalid request timing mode")
        download = data.get("download")
        material_check = data.get("material_check")
        upload = data.get("upload")
        cleanup = data.get("cleanup")
        if collect_timing and material_check is not None:
            raise ValueError("invalid request timing mode")
        if sum(value is not None for value in (download, material_check, upload, cleanup)) > 1:
            raise ValueError("ambiguous descriptor mode")
        if material_check is not None:
            if (
                not proof_request
                or data.get("instance_proof") is not None
                or data.get("request_attempt_proof") is not None
                or data.get("credential") != ""
                or data.get("instance") != ""
                or data.get("generation") is not None
                or data.get("body") != ""
                or type(material_check) is not dict
                or set(material_check) != {"size_bytes", "sha256", "reset_to"}
                or type(material_check["size_bytes"]) is not int
                or not 0 <= material_check["size_bytes"] < 2**63
                or type(material_check["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", material_check["sha256"]) is None
                or (
                    material_check["reset_to"] is not None
                    and (
                        type(material_check["reset_to"]) is not int
                        or not 0 <= material_check["reset_to"] <= material_check["size_bytes"]
                    )
                )
            ):
                raise ValueError("invalid material check")
            check_fd = _receive_output_fd(control, initial_size=material_check["size_bytes"])
            try:
                if material_check["reset_to"] is None:
                    _verify_material_file(
                        check_fd,
                        size_bytes=material_check["size_bytes"],
                        sha256=material_check["sha256"],
                    )
                else:
                    _reset_material_file(
                        check_fd,
                        size_bytes=material_check["size_bytes"],
                        reset_to=material_check["reset_to"],
                    )
            finally:
                os.close(check_fd)
            control.sendall(struct.pack("!IQ", 204, 0))
            return 0
        if cleanup is not None and (
            not proof_request
            or data.get("instance_proof") is None
            or data.get("request_attempt_proof") is not None
            or download is not None
            or upload is not None
            or type(cleanup) is not dict
            or set(cleanup)
            != {
                "cleanup_token",
                "intent_id",
                "observation_nonce",
                "intent_proof",
                "receipt_nonce",
                "recovery_nonce",
            }
            or type(cleanup["cleanup_token"]) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cleanup["cleanup_token"]) is None
            or (cleanup["recovery_nonce"] is None and not _canonical_uuid(cleanup["intent_id"]))
            or (
                cleanup["recovery_nonce"] is not None
                and (
                    not _canonical_uuid(cleanup["recovery_nonce"])
                    or cleanup["intent_id"] is not None
                    or cleanup["observation_nonce"] is not None
                    or cleanup["intent_proof"] is not None
                    or cleanup["receipt_nonce"] is not None
                )
            )
            or (
                cleanup["observation_nonce"] is not None
                and not _canonical_uuid(cleanup["observation_nonce"])
            )
            or (
                cleanup["intent_proof"] is not None
                and (
                    not _canonical_cleanup_proof(cleanup["intent_proof"])
                    or not _canonical_uuid(cleanup["observation_nonce"])
                )
            )
            or (
                cleanup["receipt_nonce"] is not None
                and (
                    cleanup["intent_proof"] is None
                    or not _canonical_uuid(cleanup["receipt_nonce"])
                    or cleanup["receipt_nonce"] == cleanup["observation_nonce"]
                )
            )
        ):
            raise ValueError("invalid cleanup request")
        output_fd = -1
        if download is not None:
            if (
                not proof_request
                or type(download) is not dict
                or set(download)
                != {
                    "size_bytes",
                    "sha256",
                    "claim_token",
                    "fencing_token",
                    "offset",
                    "segment_bytes",
                }
                or type(download["size_bytes"]) is not int
                or not 0 <= download["size_bytes"] < 2**63
                or type(download["sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", download["sha256"]) is None
                or type(download["claim_token"]) is not str
                or not 1 <= len(download["claim_token"]) <= 512
                or any(ord(ch) < 33 or ord(ch) > 126 for ch in download["claim_token"])
                or type(download["fencing_token"]) is not int
                or not 0 < download["fencing_token"] < 2**63
                or (download["offset"] is None) != (download["segment_bytes"] is None)
                or (
                    download["offset"] is not None
                    and (
                        type(download["offset"]) is not int
                        or type(download["segment_bytes"]) is not int
                        or download["offset"] < 0
                        or not 0 < download["segment_bytes"] <= _MATERIAL_SEGMENT_BYTES
                        or download["offset"] + download["segment_bytes"] > download["size_bytes"]
                    )
                )
            ):
                raise ValueError("invalid download")
            output_fd = _receive_output_fd(control, initial_size=download["offset"] or 0)
        elif upload is not None:
            if (
                not proof_request
                or data.get("instance_proof") is None
                or data.get("request_attempt_proof") is None
                or data.get("body") != ""
                or type(upload) is not dict
                or set(upload)
                != {"size_bytes", "intent_id", "observation_nonce", "result_proof", "receipt_nonce"}
                or type(upload["size_bytes"]) is not int
                or not 0 < upload["size_bytes"] < 2**63
                or (
                    upload["intent_id"] is not None
                    and (
                        not _canonical_uuid(upload["intent_id"])
                        or not _canonical_uuid(upload["observation_nonce"])
                    )
                )
                or not _canonical_uuid(upload["observation_nonce"])
                or (
                    upload["result_proof"] is not None
                    and (
                        not _canonical_result_proof(upload["result_proof"])
                        or not _canonical_uuid(upload["intent_id"])
                        or not _canonical_uuid(upload["observation_nonce"])
                    )
                )
                or (
                    upload["receipt_nonce"] is not None
                    and (
                        upload["result_proof"] is None
                        or not _canonical_uuid(upload["receipt_nonce"])
                        or upload["receipt_nonce"] == upload["observation_nonce"]
                    )
                )
            ):
                raise ValueError("invalid upload")
            output_fd = _receive_output_fd(control, sealed_size=upload["size_bytes"])
        request = urllib.request.Request(
            data["url"],
            data=(
                None
                if download is not None
                else _PreadBody(output_fd, upload["size_bytes"])
                if upload is not None
                else data["body"].encode("utf-8")
            ),
            method="GET" if download is not None else "POST",
        )
        if download is None:
            request.add_header("Content-Type", "application/json")
            if cleanup is not None:
                request.add_header("X-DLR-Cleanup-Token", cleanup["cleanup_token"])
                if cleanup["intent_id"] is not None:
                    request.add_header("X-DLR-Cleanup-Intent", cleanup["intent_id"])
                if cleanup["recovery_nonce"] is not None:
                    request.add_header("X-DLR-Result-Recovery-Nonce", cleanup["recovery_nonce"])
                if cleanup["observation_nonce"] is not None:
                    request.add_header(
                        "X-DLR-Cleanup-Observation-Nonce", cleanup["observation_nonce"]
                    )
                if cleanup["intent_proof"] is not None:
                    request.add_header("X-DLR-Cleanup-Proof", cleanup["intent_proof"])
                if cleanup["receipt_nonce"] is not None:
                    request.add_header("X-DLR-Cleanup-Receipt-Nonce", cleanup["receipt_nonce"])
            if upload is not None:
                request.add_header("Content-Length", str(upload["size_bytes"]))
                if upload["intent_id"] is not None:
                    request.add_header("X-DLR-Result-Intent", upload["intent_id"])
                request.add_header("X-DLR-Result-Observation-Nonce", upload["observation_nonce"])
                if upload["result_proof"] is not None:
                    request.add_header("X-DLR-Result-Proof", upload["result_proof"])
                if upload["receipt_nonce"] is not None:
                    request.add_header("X-DLR-Result-Receipt-Nonce", upload["receipt_nonce"])
        else:
            request.add_header("Accept-Encoding", "identity")
            request.add_header("X-DLR-Claim-Token", download["claim_token"])
            request.add_header("X-DLR-Fencing-Token", str(download["fencing_token"]))
            if download["offset"] is not None:
                end = download["offset"] + download["segment_bytes"] - 1
                request.add_header("Range", f"bytes={download['offset']}-{end}")
                request.add_header("X-DLR-Experimental-Material-SHA256", download["sha256"])
        request.add_header("Authorization", "Bearer " + data["credential"])
        request.add_header("X-DLR-Worker-Instance", data["instance"])
        if data["generation"] is not None:
            request.add_header("X-DLR-Worker-Generation", str(data["generation"]))
        instance_proof = data.get("instance_proof")
        if instance_proof is not None:
            if type(instance_proof) is not str or response_limit is None:
                raise ValueError("invalid proof mode")
            request.add_header("X-DLR-Worker-Lease-Proof", instance_proof)
        attempt_proof = data.get("request_attempt_proof")
        if attempt_proof is not None:
            if not proof_request or type(attempt_proof) is not str:
                raise ValueError("invalid attempt proof")
            request.add_header("X-DLR-Worker-Attempt-Proof", attempt_proof)
        proposal = data.get("claim_proposal")
        if proposal is not None:
            if (
                not proof_request
                or instance_proof is None
                or attempt_proof is not None
                or type(proposal) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", proposal) is None
                or download is not None
                or material_check is not None
                or upload is not None
                or cleanup is not None
                or not parts.path.endswith("/v4/claim/finalize")
            ):
                raise ValueError("invalid claim proposal")
            request.add_header("X-DLR-Claim-Proposal", proposal)
        if response_limit is not None and not proof_request:
            request.add_header("X-DLR-Worker-Claim-Wire-Bytes", str(response_limit))
        proof = b""
        try:
            if upload is not None:
                boot_clock = time.CLOCK_BOOTTIME
                if time.clock_gettime_ns(boot_clock) >= deadline_ns:
                    raise TimeoutError
            handlers: list[Any] = [_NoRedirect()]
            if request.type == "https":
                context = ssl.create_default_context()
                if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                    raise OSError("v4 TLS verification unavailable")
                handlers.append(urllib.request.HTTPSHandler(context=context))
            else:
                handlers.append(urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)
            request_start_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            with opener.open(request, timeout=data["socket_timeout"]) as response:
                if not proof_request:
                    _require_wire_ack(response, response_limit)
                if instance_proof is not None and not proof_request:
                    proof = _read_attempt_proof(
                        response, data["attempt_proof_text_length"], data["attempt_proof_bytes"]
                    )
                status = int(response.status)
                if download is not None and status == 200 and download["offset"] is None:
                    _download(
                        response,
                        output_fd,
                        size_bytes=download["size_bytes"],
                        sha256=download["sha256"],
                    )
                    body: bytes | bytearray = b""
                elif download is not None and status == 206 and download["offset"] is not None:
                    _download_segment(
                        response,
                        output_fd,
                        size_bytes=download["size_bytes"],
                        sha256=download["sha256"],
                        offset=download["offset"],
                        segment_bytes=download["segment_bytes"],
                    )
                    body = b""
                else:
                    body = _read_response(response, response_limit)
        except urllib.error.HTTPError as error:
            try:
                if not proof_request:
                    _require_wire_ack(error, response_limit)
                status = error.code
                error_limit = min(4096, response_limit) if response_limit is not None else 4096
                try:
                    body = _read_response(error, error_limit)
                except ValueError:
                    body = b""
            finally:
                with suppress(Exception):
                    error.close()
        finally:
            response_end_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            if output_fd >= 0:
                os.close(output_fd)
        if collect_timing:
            control.sendall(
                struct.pack("!IQQQ", status, len(body), request_start_ns, response_end_ns)
            )
        elif instance_proof is not None and not proof_request:
            control.sendall(struct.pack("!IIQ", status, len(proof), len(body)))
            control.sendall(proof)
        else:
            control.sendall(struct.pack("!IQ", status, len(body)))
        control.sendall(body)
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except BaseException:
        # Child diagnostics never contain URLs, tokens, responses or tracebacks.
        status = 1
    os._exit(status)
