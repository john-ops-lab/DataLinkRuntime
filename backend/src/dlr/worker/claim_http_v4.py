"""Linux pidfd-supervised Claim and proof HTTP calls with a whole-operation deadline.

Only the isolated child performs network IO. The original child is killed via
pidfd and reaped, and its creator thread is joined, before a successful or
timeout result is returned. If either cannot be established, intake remains
uncertain. Parent death triggers a kernel death signal before the child receives
credentials. This module does not ACK a broker delivery or decide whether a
remote transaction committed.
"""

from __future__ import annotations

import fcntl
import io
import json
import math
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

from dlr.common.attempt_lease_proof_wire_v4 import (
    ATTEMPT_PROOF_BYTES,
    ATTEMPT_PROOF_HEADER_WIRE_BYTES,
    ATTEMPT_PROOF_TOKEN_TEXT_LENGTH,
    parse_untrusted_attempt_proof,
)
from dlr.common.cleanup_intent_proof_v4 import verify_cleanup_intent_proof
from dlr.common.lease_proof_wire_v4 import parse_untrusted_proof, verify_worker_proof_signature
from dlr.common.material_transfer_v4 import MATERIAL_SEGMENT_BYTES
from dlr.common.result_intent_proof_wire_v4 import parse_untrusted_result_intent_proof
from dlr.common.worker_protocol_v4 import validate_control_url


@dataclass(frozen=True)
class ClaimHTTPResult:
    state: Literal["response", "unknown", "io_unknown", "blocked"]
    status: int | None = None
    body: bytes = field(default=b"", repr=False)
    attempt_proof: str | None = field(default=None, repr=False)
    request_interval_ns: tuple[int, int] | None = None


@dataclass(eq=False)
class _Unresolved:
    process: subprocess.Popen[bytes]
    pidfd: int | None
    control: socket.socket | None
    spawn_release: threading.Event | None = None
    output_fd: int | None = None


@dataclass(frozen=True)
class _Download:
    """A caller-owned empty file and frozen material expectations."""

    fd: int
    size_bytes: int
    sha256: str
    claim_token: str = field(repr=False)
    fencing_token: int
    offset: int | None = None
    segment_bytes: int | None = None


@dataclass(frozen=True)
class _MaterialCheck:
    """A caller-owned file to hash or reset without a network request."""

    fd: int
    size_bytes: int
    sha256: str
    reset_to: int | None = None


@dataclass(frozen=True)
class _Upload:
    """A sealed caller-owned request snapshot; not an IO or budget receipt."""

    fd: int
    size_bytes: int
    intent_id: str | None = None
    observation_nonce: str | None = None
    result_proof: str | None = field(default=None, repr=False)
    receipt_nonce: str | None = None


@dataclass(frozen=True)
class _Cleanup:
    """Small private cleanup headers for an isolated proof request."""

    cleanup_token: str = field(repr=False)
    verification_key: bytes = field(repr=False)
    intent_id: str | None
    observation_nonce: str | None = None
    intent_proof: str | None = field(default=None, repr=False)
    receipt_nonce: str | None = None
    intent_instance_proof: str | None = field(default=None, repr=False)
    recovery_nonce: str | None = None


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = UUID(value)
        return parsed.int != 0 and str(parsed) == value
    except ValueError:
        return False


def _valid_upload_identity(upload: _Upload) -> bool:
    if upload.intent_id is None:
        return bool(
            _canonical_uuid(upload.observation_nonce)
            and upload.result_proof is None
            and upload.receipt_nonce is None
        )
    if not _canonical_uuid(upload.intent_id) or not _canonical_uuid(upload.observation_nonce):
        return False
    if upload.result_proof is None:
        return upload.receipt_nonce is None
    claims = parse_untrusted_result_intent_proof(upload.result_proof)
    if (
        claims is None
        or str(claims.intent_id) != upload.intent_id
        or str(claims.observation_nonce) != upload.observation_nonce
    ):
        return False
    return bool(
        upload.receipt_nonce is None
        or (
            _canonical_uuid(upload.receipt_nonce)
            and upload.receipt_nonce != upload.observation_nonce
        )
    )


def _valid_cleanup_identity(cleanup: _Cleanup, instance_proof: object) -> bool:
    if (
        type(cleanup.cleanup_token) is not str
        or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cleanup.cleanup_token) is None
        or type(cleanup.verification_key) is not bytes
        or len(cleanup.verification_key) != 32
        or (
            cleanup.observation_nonce is not None and not _canonical_uuid(cleanup.observation_nonce)
        )
    ):
        return False
    if cleanup.recovery_nonce is not None:
        return bool(
            _canonical_uuid(cleanup.recovery_nonce)
            and cleanup.intent_id is None
            and cleanup.observation_nonce is None
            and cleanup.intent_proof is None
            and cleanup.receipt_nonce is None
            and cleanup.intent_instance_proof is None
            and verify_worker_proof_signature(instance_proof, cleanup.verification_key) is not None
        )
    if not _canonical_uuid(cleanup.intent_id):
        return False
    if cleanup.intent_proof is None:
        return cleanup.receipt_nonce is None and cleanup.intent_instance_proof is None
    claims = verify_cleanup_intent_proof(
        cleanup.intent_proof,
        public_key=cleanup.verification_key,
        instance_token=cleanup.intent_instance_proof,
    )
    original = verify_worker_proof_signature(
        cleanup.intent_instance_proof, cleanup.verification_key
    )
    current = verify_worker_proof_signature(instance_proof, cleanup.verification_key)
    return bool(
        claims is not None
        and original is not None
        and current is not None
        and current.cluster_id == original.cluster_id
        and current.authority_epoch == original.authority_epoch
        and current.worker_id == original.worker_id
        and current.worker_identity_id == original.worker_identity_id
        and current.instance_id == original.instance_id
        and current.generation == original.generation
        and current.revision >= original.revision
        and (current.revision > original.revision or current.candidate_id == original.candidate_id)
        and str(claims.intent_id) == cleanup.intent_id
        and str(claims.observation_nonce) == cleanup.observation_nonce
        and (
            cleanup.receipt_nonce is None
            or (
                _canonical_uuid(cleanup.receipt_nonce)
                and cleanup.receipt_nonce != cleanup.observation_nonce
            )
        )
    )


def _result_seals() -> int:
    seals = 0
    for name in ("F_SEAL_WRITE", "F_SEAL_GROW", "F_SEAL_SHRINK", "F_SEAL_SEAL"):
        value = getattr(fcntl, name, None)
        if type(value) is not int:
            raise ValueError("sealed request unavailable")
        seals |= value
    return seals


_unresolved_lock = threading.Lock()
_unresolved: set[_Unresolved] = set()
_retention_lost = False
_owner_pid = os.getpid()


def _retain(
    process: subprocess.Popen[bytes],
    pidfd: int | None,
    control: socket.socket | None,
    spawn_release: threading.Event | None = None,
    output_fd: int | None = None,
) -> None:
    global _retention_lost
    with _unresolved_lock:
        try:
            _unresolved.add(_Unresolved(process, pidfd, control, spawn_release, output_fd))
        except BaseException as error:
            # An interrupted or failed retention cannot make an unconfirmed
            # child safe to replace. Latch intake before propagating signals.
            _retention_lost = True
            if not isinstance(error, Exception):
                raise


def _clock() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined, unused-ignore]


def _check_cancelled(events: tuple[threading.Event, ...]) -> None:
    if any(event.is_set() for event in events):
        raise InterruptedError("material authority stopped")


def _wait_socket(
    sock: socket.socket,
    *,
    write: bool,
    deadline: float,
    cancellation_events: tuple[threading.Event, ...] = (),
) -> None:
    while True:
        _check_cancelled(cancellation_events)
        remaining = deadline - _clock()
        if remaining <= 0:
            raise TimeoutError
        readers, writers, _ = select.select(
            [] if write else [sock], [sock] if write else [], [], min(remaining, 0.1)
        )
        if readers or writers:
            _check_cancelled(cancellation_events)
            if _clock() >= deadline:
                raise TimeoutError
            return


def _send_all(
    sock: socket.socket,
    value: bytes,
    deadline: float,
    cancellation_events: tuple[threading.Event, ...] = (),
) -> None:
    view = memoryview(value)
    while view:
        _wait_socket(sock, write=True, deadline=deadline, cancellation_events=cancellation_events)
        try:
            count = sock.send(view)
        except BlockingIOError:
            continue
        if count <= 0:
            raise OSError("control closed")
        view = view[count:]


def _send_fd(
    sock: socket.socket,
    fd: int,
    deadline: float,
    cancellation_events: tuple[threading.Event, ...] = (),
    authorize: Callable[[Callable[[], None]], bool] | None = None,
) -> None:
    """Send one descriptor, optionally fencing its nonblocking gate write."""
    while True:
        _wait_socket(sock, write=True, deadline=deadline, cancellation_events=cancellation_events)

        def send_once() -> None:
            _check_cancelled(cancellation_events)
            if _clock() >= deadline:
                raise TimeoutError
            sent = sock.sendmsg(
                [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))]
            )
            if sent != 1:
                raise OSError("descriptor transfer failed")

        try:
            if authorize is None:
                send_once()
            elif not authorize(send_once):
                raise PermissionError("descriptor authority unavailable")
        except BlockingIOError:
            continue
        return


def _receive(
    sock: socket.socket,
    length: int,
    deadline: float,
    cancellation_events: tuple[threading.Event, ...] = (),
) -> bytes:
    # Keep the immutable return contract without explicitly copying a full
    # bytearray. This optimization is not a process-memory bound.
    result = io.BytesIO()
    received = 0
    while received < length:
        _wait_socket(sock, write=False, deadline=deadline, cancellation_events=cancellation_events)
        try:
            data = sock.recv(min(65536, length - received))
        except BlockingIOError:
            continue
        if not data:
            raise OSError("control closed")
        if result.write(data) != len(data):
            raise OSError("short receive buffer write")
        received += len(data)
    return result.getvalue()


def _reap(
    process: subprocess.Popen[bytes],
    pidfd: int,
    deadline: float,
    cancellation_events: tuple[threading.Event, ...] = (),
) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while True:
        _check_cancelled(cancellation_events)
        remaining = deadline - _clock()
        if remaining <= 0:
            return False
        if not poller.poll(max(1, math.ceil(min(remaining, 0.1) * 1000))):
            continue
        try:
            result = os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)  # type: ignore[attr-defined, unused-ignore]
        except (OSError, ChildProcessError):
            return False
        if result is None:
            continue
        if result.si_pid != process.pid:
            return False
        process.returncode = (
            result.si_status if result.si_code == os.CLD_EXITED else -result.si_status
        )
        return True


@dataclass(eq=False)
class _Spawn:
    done: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: subprocess.Popen[bytes] | None = None
    child: socket.socket | None = None
    abandoned: bool = False
    thread: threading.Thread | None = None


_spawning_lock = threading.Lock()
_spawning: set[_Spawn] = set()


def _spawn_until(
    ticket: _Spawn,
    deadline: float,
    deadline_ns: int,
    address_space_limit: int | None,
    cancellation_events: tuple[threading.Event, ...] = (),
    *,
    network_env: bool = True,
    _process_command: list[str] | None = None,
    _process_env: dict[str, str] | None = None,
) -> None:
    """Bound spawn without closing or reusing its inherited fd mid-exec.

    Linux parent-death signaling is tied to the creating thread, so that
    thread stays alive until the child is reaped or explicitly retained.
    An abandoned spawn never receives credentials or makes an HTTP request.
    """
    child = ticket.child
    assert child is not None
    with _spawning_lock:
        _spawning.add(ticket)

    def run() -> None:
        process = None
        try:
            env = (
                {
                    key: value
                    # HTTP requests need the configured TLS/proxy environment.
                    for key, value in os.environ.items()
                    if key in {"SSL_CERT_FILE", "SSL_CERT_DIR", "REQUEST_METHOD"}
                    or key.lower() in {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
                }
                if network_env
                else {}
            )
            if _process_env is not None:
                env = dict(_process_env)
            if _process_command is None:
                command = [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(__file__).with_name("claim_http_child_v4.py")),
                    str(child.fileno()),
                    str(os.getpid()),
                    str(deadline_ns),
                ]
                if address_space_limit is not None:
                    command.append(str(address_space_limit))
            else:
                command = list(_process_command)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                close_fds=True,
                pass_fds=(child.fileno(),),
            )
        except Exception:
            pass
        finally:
            child.close()
        with ticket.lock:
            ticket.child = None
            abandoned = ticket.abandoned
            ticket.process = process
            ticket.done.set()
        if not abandoned and process is not None:
            ticket.release.wait()
            with ticket.lock:
                abandoned = ticket.abandoned
        if abandoned and process is not None:
            # No payload was sent. End the original child without guessing a PID.
            try:
                fd = os.pidfd_open(process.pid, 0)  # type: ignore[attr-defined, unused-ignore]
            except BaseException as error:
                _retain(process, None, None)
                if not isinstance(error, Exception):
                    raise
            else:
                reaped = False
                try:
                    with suppress(Exception):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)  # type: ignore[attr-defined, unused-ignore]
                    try:
                        reaped = _reap(process, fd, _clock() + 2.0)
                    except Exception:
                        reaped = False
                finally:
                    if reaped:
                        os.close(fd)
                    else:
                        _retain(process, fd, None)
        with _spawning_lock:
            _spawning.discard(ticket)

    try:
        ticket.thread = threading.Thread(target=run, name="dlr-v4-claim-spawn", daemon=True)
        ticket.thread.start()
    except BaseException as error:
        # A patched or failing start may have launched the thread before
        # raising. It owns the descriptor until it confirms completion.
        with ticket.lock:
            ticket.abandoned = True
        ticket.release.set()
        if isinstance(error, Exception):
            return
        raise
    while True:
        if any(event.is_set() for event in cancellation_events):
            with ticket.lock:
                ticket.abandoned = True
            ticket.release.set()
            return
        try:
            remaining = deadline - _clock()
        except Exception:
            remaining = 0
        if remaining <= 0:
            with ticket.lock:
                if not ticket.done.is_set():
                    ticket.abandoned = True
                    ticket.release.set()
                    return
                return
        if ticket.done.wait(min(remaining, 0.1)):
            return


class _StopClaim(Exception):
    def __init__(self, result: ClaimHTTPResult) -> None:
        self.result = result


def _join_creator(ticket: _Spawn) -> bool:
    """Release and join the creator after the child is known to be reaped."""
    try:
        ticket.release.set()
        thread = ticket.thread
        if thread is None:
            return False
        deadline = _clock() + 2.0
        while thread.is_alive():
            remaining = deadline - _clock()
            if remaining <= 0:
                return False
            thread.join(min(remaining, 0.1))
        # A creator interrupted after publishing its child may exit without
        # removing its ticket. The child is already reaped at this point.
        with _spawning_lock:
            _spawning.discard(ticket)
        return True
    except Exception:
        return False


def request_claim(
    *,
    url: str,
    credential: str,
    instance: str,
    generation: int | None,
    body: str,
    timeout_seconds: float,
    _response_limit_bytes: int | None = None,
    _child_address_space_bytes: int | None = None,
    _instance_proof: str | None = None,
    _test_only_plaintext_loopback: bool = False,
    _proof_request: bool = False,
    _collect_request_timing: bool = False,
    _attempt_proof: str | None = None,
    _claim_proposal: str | None = None,
    _absolute_deadline: float | None = None,
    _download: _Download | None = None,
    _material_check: _MaterialCheck | None = None,
    _upload: _Upload | None = None,
    _cleanup: _Cleanup | None = None,
    _upload_authorize: Callable[[Callable[[], None]], bool] | None = None,
    _cancellation_events: tuple[threading.Event, ...] = (),
) -> ClaimHTTPResult:
    """Perform a single non-retried Claim and end all child IO on timeout.

    The deadline includes spawn, transfer, response and child exit, measured
    using Linux CLOCK_BOOTTIME. Termination/reaping may take an additional two
    seconds, as may joining the creator thread. Unresolved handles are
    retained rather than treated as stopped.
    The optional experimental byte ceiling is not a memory reservation or a
    negotiated deployment capability. An oversized response stays unknown:
    the remote Claim may already have committed.
    An optional child address-space limit excludes the Agent and is not a
    cgroup, a complete memory budget or proof of parser memory use.
    """
    global _retention_lost
    # Never enter inherited locks or reuse a parent's IO responsibility after
    # fork. The new process must initialize its own intake and recovery gate.
    if os.getpid() != _owner_pid:
        return ClaimHTTPResult("blocked")
    # A creator may still publish an unresolved child while it leaves the
    # spawning set. Observe both sets together to avoid an admission gap.
    with _spawning_lock, _unresolved_lock:
        if _unresolved or _retention_lost:
            return ClaimHTTPResult("io_unknown")
        for pending in _spawning:
            with pending.lock:
                if pending.abandoned:
                    return ClaimHTTPResult("io_unknown")
    try:
        validate_control_url(
            url, _test_only_plaintext_loopback=_test_only_plaintext_loopback, allow_query=True
        )
    except ValueError:
        return ClaimHTTPResult("blocked")
    if (
        not sys.platform.startswith("linux")
        or type(_collect_request_timing) is not bool
        or (_collect_request_timing and (not _proof_request or _material_check is not None))
        or not all(hasattr(os, x) for x in ("pidfd_open", "P_PIDFD"))
        or not hasattr(signal, "pidfd_send_signal")
        or type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= (86_400 if _material_check is not None else 60)
        or (
            _response_limit_bytes is not None
            and (type(_response_limit_bytes) is not int or not 0 < _response_limit_bytes < 2**63)
        )
        or (
            _child_address_space_bytes is not None
            and (
                type(_child_address_space_bytes) is not int
                or not 0 < _child_address_space_bytes < 2**63
            )
        )
        or (
            _instance_proof is not None
            and (_response_limit_bytes is None or parse_untrusted_proof(_instance_proof) is None)
        )
        or type(_proof_request) is not bool
        or (
            _attempt_proof is not None
            and (not _proof_request or parse_untrusted_attempt_proof(_attempt_proof) is None)
        )
        or (
            _claim_proposal is not None
            and (
                not _proof_request
                or _instance_proof is None
                or _attempt_proof is not None
                or type(_claim_proposal) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", _claim_proposal) is None
                or _download is not None
                or _material_check is not None
                or _upload is not None
                or _cleanup is not None
            )
        )
        or (not _proof_request and generation is None)
        or (
            _download is not None
            and (
                type(_download) is not _Download
                or not _proof_request
                or _material_check is not None
            )
        )
        or (
            _material_check is not None
            and (
                type(_material_check) is not _MaterialCheck
                or not _proof_request
                or _instance_proof is not None
                or _attempt_proof is not None
                or _upload is not None
                or _cleanup is not None
                or body != ""
            )
        )
        or (
            _upload is not None
            and (
                type(_upload) is not _Upload
                or not _proof_request
                or _instance_proof is None
                or _attempt_proof is None
                or _download is not None
                or type(_upload.fd) is not int
                or _upload.fd < 3
                or type(_upload.size_bytes) is not int
                or not 0 < _upload.size_bytes < 2**63
                or body != ""
                or not callable(_upload_authorize)
                or not _valid_upload_identity(_upload)
            )
        )
        or (_upload is None and _upload_authorize is not None)
        or (
            _cleanup is not None
            and (
                type(_cleanup) is not _Cleanup
                or not _proof_request
                or _instance_proof is None
                or _attempt_proof is not None
                or _download is not None
                or _upload is not None
                or _absolute_deadline is None
                or not _valid_cleanup_identity(_cleanup, _instance_proof)
            )
        )
        or type(_cancellation_events) is not tuple
        or any(not isinstance(event, threading.Event) for event in _cancellation_events)
        or (
            _absolute_deadline is not None
            and (
                type(_absolute_deadline) not in (int, float)
                or not math.isfinite(_absolute_deadline)
                or _absolute_deadline <= 0
            )
        )
    ):
        return ClaimHTTPResult("blocked")
    if _response_limit_bytes is not None and _child_address_space_bytes is not None:
        # Retain the conservative half-address-space ceiling even though the
        # child no longer explicitly copies the completed response. Interpreter,
        # TLS, allocator and parent memory remain outside this simple bound.
        _response_limit_bytes = min(_response_limit_bytes, _child_address_space_bytes // 2)
        # The experimental endpoint needs this much for control decisions.
        if _response_limit_bytes < 4096:
            return ClaimHTTPResult("blocked")
    parent: socket.socket | None = None
    child: socket.socket | None = None
    output_fd = -1
    output_transferred = False
    output_identity: tuple[int, int] | None = None
    check_fingerprint: tuple[int, ...] | None = None
    try:
        if _download is not None:
            output_fd = os.dup(_download.fd)
            info = os.fstat(output_fd)
            output_identity = (info.st_dev, info.st_ino)
            flags = fcntl.fcntl(output_fd, fcntl.F_GETFL)
            expected_offset = _download.offset if _download.offset is not None else 0
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != expected_offset
                or flags & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR)
                or flags & os.O_APPEND
                or os.lseek(output_fd, 0, os.SEEK_CUR) != expected_offset
            ):
                raise ValueError("output file unavailable")
        elif _material_check is not None:
            output_fd = os.dup(_material_check.fd)
            info = os.fstat(output_fd)
            flags = fcntl.fcntl(output_fd, fcntl.F_GETFL)
            check_fingerprint = (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != _material_check.size_bytes
                or flags & os.O_ACCMODE != os.O_RDWR
                or flags & os.O_APPEND
                or os.lseek(output_fd, 0, os.SEEK_CUR) != _material_check.size_bytes
            ):
                raise ValueError("material file unavailable")
        elif _upload is not None:
            output_fd = os.dup(_upload.fd)
            info = os.fstat(output_fd)
            flags = fcntl.fcntl(output_fd, fcntl.F_GETFL)
            seals = _result_seals()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 0
                or info.st_size != _upload.size_bytes
                or flags & os.O_ACCMODE not in (os.O_RDONLY, os.O_RDWR)
                or flags & os.O_APPEND
                or fcntl.fcntl(output_fd, fcntl.F_GET_SEALS) & seals != seals
            ):
                raise ValueError("sealed request unavailable")
        started = _clock()
        deadline = started + float(timeout_seconds)
        if _absolute_deadline is not None:
            deadline = min(deadline, _absolute_deadline)
            if deadline <= _clock():
                raise TimeoutError
        numerator, denominator = deadline.as_integer_ratio()
        deadline_ns = numerator * 1_000_000_000 // denominator
        encoded = json.dumps(
            {
                "url": url,
                "credential": credential,
                "instance": instance,
                "generation": generation,
                "body": body,
                "socket_timeout": timeout_seconds,
                "response_limit_bytes": _response_limit_bytes,
                "instance_proof": _instance_proof,
                "proof_request": _proof_request,
                "collect_request_timing": _collect_request_timing,
                "request_attempt_proof": _attempt_proof,
                "claim_proposal": _claim_proposal,
                "test_only_plaintext_loopback": _test_only_plaintext_loopback,
                "attempt_proof_text_length": ATTEMPT_PROOF_TOKEN_TEXT_LENGTH,
                "attempt_proof_bytes": ATTEMPT_PROOF_BYTES,
                "download": (
                    {
                        "size_bytes": _download.size_bytes,
                        "sha256": _download.sha256,
                        "claim_token": _download.claim_token,
                        "fencing_token": _download.fencing_token,
                        "offset": _download.offset,
                        "segment_bytes": _download.segment_bytes,
                    }
                    if _download is not None
                    else None
                ),
                "material_check": (
                    {
                        "size_bytes": _material_check.size_bytes,
                        "sha256": _material_check.sha256,
                        "reset_to": _material_check.reset_to,
                    }
                    if _material_check is not None
                    else None
                ),
                "upload": (
                    {
                        "size_bytes": _upload.size_bytes,
                        "intent_id": _upload.intent_id,
                        "observation_nonce": _upload.observation_nonce,
                        "result_proof": _upload.result_proof,
                        "receipt_nonce": _upload.receipt_nonce,
                    }
                    if _upload is not None
                    else None
                ),
                "cleanup": (
                    {
                        "cleanup_token": _cleanup.cleanup_token,
                        "intent_id": _cleanup.intent_id,
                        "observation_nonce": _cleanup.observation_nonce,
                        "intent_proof": _cleanup.intent_proof,
                        "receipt_nonce": _cleanup.receipt_nonce,
                        "recovery_nonce": _cleanup.recovery_nonce,
                    }
                    if _cleanup is not None
                    else None
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        parent, child = socket.socketpair()
        parent.setblocking(False)
    except Exception:
        if parent is not None:
            parent.close()
        if child is not None:
            child.close()
        if output_fd >= 0:
            os.close(output_fd)
        return ClaimHTTPResult("blocked")
    assert parent is not None and child is not None
    process = None
    pidfd = None
    ticket = None
    result = ClaimHTTPResult("unknown")
    reaped = False
    try:
        _check_cancelled(_cancellation_events)
        # The caller retains the ticket even if spawn is interrupted. Once
        # transferred, only the creator may close the descriptor.
        ticket = _Spawn(child=child)
        child = None
        _spawn_until(
            ticket,
            deadline,
            deadline_ns,
            _child_address_space_bytes,
            _cancellation_events,
            network_env=_material_check is None,
        )
        with ticket.lock:
            if ticket.abandoned:
                raise _StopClaim(ClaimHTTPResult("io_unknown"))
            if ticket.process is None:
                raise _StopClaim(ClaimHTTPResult("blocked"))
        process = ticket.process
        try:
            pidfd = os.pidfd_open(process.pid, 0)
        except BaseException as error:
            # No credentials have been sent. EOF prevents any HTTP request.
            result = ClaimHTTPResult("io_unknown")
            _retain(process, None, None, ticket.release)
            with suppress(Exception):
                parent.close()
            ticket.release.set()
            if isinstance(error, Exception):
                raise _StopClaim(result) from None
            raise
        if _receive(parent, 1, deadline, _cancellation_events) != b"R":
            raise ValueError("invalid child")
        # R is sent only after the child installs its parent-death signal.
        # Linux binds that signal to the creator thread, not the process;
        # getppid() alone cannot detect its earlier death while another parent
        # thread remains alive. Never send credentials through that gap.
        if ticket.thread is None or not ticket.thread.is_alive():
            raise ValueError("creator thread unavailable")
        _send_all(parent, struct.pack("!Q", len(encoded)) + encoded, deadline, _cancellation_events)
        if output_fd >= 0:
            # Once transfer is attempted, an interrupted send is ambiguous.
            output_transferred = True
            _send_fd(parent, output_fd, deadline, _cancellation_events, _upload_authorize)
        proof: str | None = None
        request_interval = None
        if _collect_request_timing:
            status, length, request_start_ns, response_end_ns = struct.unpack(
                "!IQQQ", _receive(parent, 28, deadline, _cancellation_events)
            )
            # The trusted IO child uses this same CLOCK_BOOTTIME/time namespace.
            # Reject malformed or out-of-operation observations before using
            # the tighter interval; the outer deadline still includes reaping.
            if not (
                math.floor(started * 1_000_000_000)
                <= request_start_ns
                <= response_end_ns
                <= math.ceil(_clock() * 1_000_000_000)
            ):
                raise ValueError("invalid request timing")
            request_interval = (request_start_ns, response_end_ns)
        elif _instance_proof is not None and not _proof_request:
            status, proof_length, length = struct.unpack(
                "!IIQ", _receive(parent, 16, deadline, _cancellation_events)
            )
            if proof_length not in (0, ATTEMPT_PROOF_TOKEN_TEXT_LENGTH):
                raise ValueError("invalid proof response")
            if proof_length:
                if (
                    _response_limit_bytes is None
                    or length + ATTEMPT_PROOF_HEADER_WIRE_BYTES > _response_limit_bytes
                ):
                    raise ValueError("response exceeds wire budget")
                proof = _receive(parent, proof_length, deadline, _cancellation_events).decode(
                    "ascii"
                )
        else:
            status, length = struct.unpack(
                "!IQ", _receive(parent, 12, deadline, _cancellation_events)
            )
        if not 100 <= status <= 599:
            raise ValueError("invalid status")
        if _response_limit_bytes is not None and length > _response_limit_bytes:
            raise ValueError("response exceeds wire budget")
        raw = _receive(parent, length, deadline, _cancellation_events)
        if not _reap(process, pidfd, deadline, _cancellation_events):
            raise TimeoutError
        reaped = True
        if _clock() >= deadline:
            raise TimeoutError
        if process.returncode != 0:
            raise ValueError("child failed")
        if _download is not None and (
            (status == 200 and _download.offset is None)
            or (status == 206 and _download.offset is not None)
        ):
            info = os.fstat(output_fd)
            expected_size = (
                _download.size_bytes
                if _download.offset is None
                else _download.offset + (_download.segment_bytes or 0)
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != output_identity
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size != expected_size
                or os.lseek(output_fd, 0, os.SEEK_CUR) != expected_size
            ):
                raise ValueError("output file changed")
        if _material_check is not None and status == 204:
            info = os.fstat(output_fd)
            after = (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if (
                check_fingerprint is None
                or (_material_check.reset_to is None and after != check_fingerprint)
                or (
                    _material_check.reset_to is not None
                    and (
                        after[:5] != check_fingerprint[:5]
                        or after[5] != _material_check.reset_to
                        or os.lseek(output_fd, 0, os.SEEK_CUR) != _material_check.reset_to
                    )
                )
            ):
                raise ValueError("material file changed")
        result = ClaimHTTPResult("response", status, raw, proof, request_interval)
    except _StopClaim as stopped:
        result = stopped.result
    except Exception:
        result = ClaimHTTPResult("blocked") if process is None else ClaimHTTPResult("unknown")
    finally:
        if child is not None:
            child.close()
        if ticket is not None and process is None:
            with ticket.lock:
                if not ticket.done.is_set() or ticket.process is not None:
                    ticket.abandoned = True
                    ticket.release.set()
                    result = ClaimHTTPResult("io_unknown")
                if ticket.thread is None and ticket.child is not None:
                    ticket.child.close()
                    ticket.child = None
        if process is not None and pidfd is not None and not reaped:
            try:
                with suppress(Exception):
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                try:
                    reaped = _reap(process, pidfd, _clock() + 2.0)
                except Exception:
                    reaped = False
            finally:
                # Retain even if cancellation interrupts signal or reap. The
                # exact pidfd remains authoritative; never guess a new PID.
                if not reaped:
                    _retain(
                        process,
                        pidfd,
                        parent,
                        ticket.release if ticket is not None else None,
                        output_fd if output_transferred else None,
                    )
                    if output_transferred:
                        output_fd = -1
                    pidfd = None
                    parent = None
                    result = ClaimHTTPResult("io_unknown")
        if parent is not None:
            parent.close()
        if pidfd is not None:
            os.close(pidfd)
        if output_fd >= 0:
            os.close(output_fd)
        if ticket is not None and result.state != "io_unknown" and not _join_creator(ticket):
            # The original process has already been reaped (or no process
            # was created). Do not signal a possibly reused numeric PID.
            # Its creator may still retain memory or descriptors, so no
            # new Claim can rely on IO having ended.
            with _unresolved_lock:
                _retention_lost = True
            result = ClaimHTTPResult("io_unknown")
    return result


def request_proof(
    *,
    url: str,
    credential: str,
    instance: str,
    generation: int | None,
    body: str,
    instance_proof: str | None,
    attempt_proof: str | None,
    timeout_seconds: float,
    response_limit_bytes: int,
    _test_only_plaintext_loopback: bool = False,
    _absolute_deadline: float | None = None,
    _cancellation_events: tuple[threading.Event, ...] = (),
    _cleanup: _Cleanup | None = None,
    _claim_proposal: str | None = None,
) -> ClaimHTTPResult:
    """Use the same supervised child for small proof operations, without Claim framing."""
    try:
        validate_control_url(url, _test_only_plaintext_loopback=_test_only_plaintext_loopback)
        if type(body) is not str:
            raise ValueError
        body_size = len(body.encode("utf-8"))
    except (ValueError, UnicodeError):
        return ClaimHTTPResult("blocked")
    if (
        body_size > 16_384
        or type(response_limit_bytes) is not int
        or not 0 < response_limit_bytes <= 8192
        or (generation is not None and (type(generation) is not int or not 0 < generation < 2**63))
        or (generation is None) != (instance_proof is None)
        or (attempt_proof is not None and instance_proof is None)
    ):
        return ClaimHTTPResult("blocked")
    return request_claim(
        url=url,
        credential=credential,
        instance=instance,
        generation=generation,
        body=body,
        timeout_seconds=timeout_seconds,
        _response_limit_bytes=response_limit_bytes,
        _instance_proof=instance_proof,
        _test_only_plaintext_loopback=_test_only_plaintext_loopback,
        _proof_request=True,
        _attempt_proof=attempt_proof,
        _claim_proposal=_claim_proposal,
        _absolute_deadline=_absolute_deadline,
        _cancellation_events=_cancellation_events,
        _cleanup=_cleanup,
    )


def request_download(
    *,
    url: str,
    credential: str,
    instance: str,
    generation: int,
    instance_proof: str,
    attempt_proof: str,
    claim_token: str,
    fencing_token: int,
    output_fd: int,
    size_bytes: int,
    sha256: str,
    timeout_seconds: float,
    _test_only_plaintext_loopback: bool = False,
    _absolute_deadline: float | None = None,
    _cancellation_events: tuple[threading.Event, ...] = (),
    _offset: int | None = None,
    _segment_bytes: int | None = None,
) -> ClaimHTTPResult:
    """Stream one proof-bound material into an existing private empty file.

    The caller retains the file and responsibility on every failure. An
    io_unknown result also means the original child might still hold its fd;
    it is not permission to clean up or reuse the workspace.
    """
    if (
        type(output_fd) is not int
        or output_fd < 0
        or type(size_bytes) is not int
        or not 0 <= size_bytes < 2**63
        or type(sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or type(claim_token) is not str
        or not 1 <= len(claim_token) <= 512
        or any(ord(character) < 33 or ord(character) > 126 for character in claim_token)
        or type(fencing_token) is not int
        or not 0 < fencing_token < 2**63
        or type(generation) is not int
        or not 0 < generation < 2**63
        or parse_untrusted_proof(instance_proof) is None
        or parse_untrusted_attempt_proof(attempt_proof) is None
        or (_offset is None) != (_segment_bytes is None)
        or (
            _offset is not None
            and (
                type(_offset) is not int
                or type(_segment_bytes) is not int
                or _offset < 0
                or not 0 < _segment_bytes <= MATERIAL_SEGMENT_BYTES
                or _offset + _segment_bytes > size_bytes
            )
        )
    ):
        return ClaimHTTPResult("blocked")
    return request_claim(
        url=url,
        credential=credential,
        instance=instance,
        generation=generation,
        body="",
        timeout_seconds=timeout_seconds,
        _response_limit_bytes=4096,
        _instance_proof=instance_proof,
        _test_only_plaintext_loopback=_test_only_plaintext_loopback,
        _proof_request=True,
        _attempt_proof=attempt_proof,
        _absolute_deadline=_absolute_deadline,
        _download=_Download(
            output_fd, size_bytes, sha256, claim_token, fencing_token, _offset, _segment_bytes
        ),
        _cancellation_events=_cancellation_events,
    )


def verify_material_file(
    *,
    output_fd: int,
    size_bytes: int,
    sha256: str,
    timeout_seconds: float,
    _absolute_deadline: float,
    _cancellation_events: tuple[threading.Event, ...] = (),
) -> ClaimHTTPResult:
    """Hash one caller-owned file in the existing supervised, networkless child.

    A returned ``io_unknown`` retains the descriptor as unresolved local IO;
    callers must not remove or release its associated responsibility.
    """
    if (
        type(output_fd) is not int
        or output_fd < 3
        or type(size_bytes) is not int
        or not 0 <= size_bytes < 2**63
        or type(sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        return ClaimHTTPResult("blocked")
    return request_claim(
        url="https://localhost.invalid",
        credential="",
        instance="",
        generation=None,
        body="",
        timeout_seconds=timeout_seconds,
        _response_limit_bytes=4096,
        _proof_request=True,
        _absolute_deadline=_absolute_deadline,
        _material_check=_MaterialCheck(output_fd, size_bytes, sha256),
        _cancellation_events=_cancellation_events,
    )


def reset_material_file(
    *,
    output_fd: int,
    size_bytes: int,
    reset_to: int,
    sha256: str,
    timeout_seconds: float,
    _absolute_deadline: float,
    _cancellation_events: tuple[threading.Event, ...] = (),
) -> ClaimHTTPResult:
    """Discard only an unresolved segment after its writer has been reaped."""
    if (
        type(output_fd) is not int
        or output_fd < 3
        or type(size_bytes) is not int
        or not 0 <= size_bytes < 2**63
        or type(reset_to) is not int
        or not 0 <= reset_to <= size_bytes
        or type(sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        return ClaimHTTPResult("blocked")
    return request_claim(
        url="https://localhost.invalid",
        credential="",
        instance="",
        generation=None,
        body="",
        timeout_seconds=timeout_seconds,
        _response_limit_bytes=4096,
        _proof_request=True,
        _absolute_deadline=_absolute_deadline,
        _material_check=_MaterialCheck(output_fd, size_bytes, sha256, reset_to),
        _cancellation_events=_cancellation_events,
    )
