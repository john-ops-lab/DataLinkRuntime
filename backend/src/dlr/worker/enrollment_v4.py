"""One-shot fresh-identity enrollment, isolated from the Worker Agent.

The private credential directory must already exist and be exclusively managed
by a trusted operator. A durable attempt marker prevents automatic re-exchange
when the network, server commit, or credential publication becomes uncertain.
"""

import json
import math
import os
import re
import secrets
import stat
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from dlr.common.worker_protocol_v4 import (
    WORKER_CREDENTIAL_PREFIX,
    is_worker_credential,
    validate_control_url,
)
from dlr.worker.client import _open_v4

_ENROLLMENT = "enrollment.secret"
_RUNTIME = "runtime-credential.json"
_ATTEMPT = ".enrollment-attempted"
_MAX_RESPONSE = 8192


class EnrollmentError(Exception):
    """Non-sensitive error; uncertain exchanges require administrator recovery."""


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: StrictInt = Field(gt=0, le=2**63 - 1)
    worker_identity_id: UUID
    credential_id: str = Field(
        pattern=rf"^{re.escape(WORKER_CREDENTIAL_PREFIX)}[0-9a-f]{{64}}$", repr=False
    )
    credential_record_id: StrictInt = Field(gt=0, le=2**63 - 1)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _anchor(fd: int, path: Path, identity: tuple[int, int]) -> None:
    descriptor, visible = os.fstat(fd), path.lstat()
    if (
        not stat.S_ISDIR(descriptor.st_mode)
        or not stat.S_ISDIR(visible.st_mode)
        or (descriptor.st_dev, descriptor.st_ino) != identity
        or (visible.st_dev, visible.st_ino) != identity
        or descriptor.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor.st_mode) != 0o700
        or stat.S_IMODE(visible.st_mode) != 0o700
    ):
        raise ValueError("credential directory changed")


def _file(fd: int, name: str, directory: int, expected: os.stat_result) -> None:
    current = os.fstat(fd)
    visible = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if (
        not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(visible.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) != 0o600
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or (visible.st_dev, visible.st_ino) != (expected.st_dev, expected.st_ino)
        or (current.st_size, current.st_mtime_ns) != (expected.st_size, expected.st_mtime_ns)
    ):
        raise ValueError("credential file changed")


def enroll(
    *,
    base_url: str,
    worker_id: int,
    worker_identity_id: UUID,
    credential_directory: Path,
    timeout_seconds: float = 30.0,
    _test_only_plaintext_loopback: bool = False,
) -> None:
    """Exchange once and durably publish a bound credential, or stop safely.

    A successful return means a credential file was durably published; it does
    not register an instance or authorize execution. Never automatically retry.
    """
    if (
        type(worker_id) is not int
        or not 0 < worker_id <= 2**63 - 1
        or not isinstance(worker_identity_id, UUID)
        or worker_identity_id.int == 0
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 120
    ):
        raise EnrollmentError("enrollment_configuration_invalid")
    try:
        directory_path = Path(credential_directory)
        validate_control_url(base_url, _test_only_plaintext_loopback=_test_only_plaintext_loopback)
        parts = urlsplit(base_url)
        if (
            not directory_path.is_absolute()
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(char.isspace() or ord(char) < 32 for char in base_url)
        ):
            raise ValueError
        _ = parts.port
    except (TypeError, ValueError):
        raise EnrollmentError("enrollment_configuration_invalid") from None

    directory = enrollment_fd = temp_fd = -1
    temp_name: str | None = None
    attempted = False
    try:
        directory = os.open(
            directory_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        info = os.fstat(directory)
        identity = (info.st_dev, info.st_ino)
        _anchor(directory, directory_path, identity)
        enrollment_fd = os.open(
            _ENROLLMENT,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        source = os.fstat(enrollment_fd)
        secret_length = len(WORKER_CREDENTIAL_PREFIX) + 64
        if source.st_size not in {secret_length, secret_length + 1}:
            raise ValueError("invalid enrollment file")
        _file(enrollment_fd, _ENROLLMENT, directory, source)
        raw = os.read(enrollment_fd, secret_length + 2)
        _file(enrollment_fd, _ENROLLMENT, directory, source)
        token = raw.removesuffix(b"\n").decode("ascii")
        if not is_worker_credential(token) or token in base_url:
            raise ValueError("invalid enrollment file")
        # Probe before sending, but publish with an atomic no-replace link later.
        try:
            os.stat(_RUNTIME, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise EnrollmentError("enrollment_credential_exists")
        try:
            marker = os.open(
                _ATTEMPT,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            raise EnrollmentError("enrollment_recovery_required") from None
        attempted = True
        try:
            if os.write(marker, b"v1\n") != 3:
                raise OSError("short write")
            os.fsync(marker)
            _file(marker, _ATTEMPT, directory, os.fstat(marker))
        finally:
            os.close(marker)
        os.fsync(directory)
        _anchor(directory, directory_path, identity)
        _file(enrollment_fd, _ENROLLMENT, directory, source)
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/workers/{worker_id}/credentials/exchange",
            data=b"",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            transport_options = (
                {"_test_only_plaintext_loopback": True} if _test_only_plaintext_loopback else {}
            )
            with _open_v4(request, timeout=timeout_seconds, **transport_options) as response:
                if response.status != 201:
                    raise ValueError("exchange failed")
                body = response.read(_MAX_RESPONSE + 1)
        except urllib.error.HTTPError as error:
            with suppress(Exception):
                error.close()
            raise ValueError("exchange failed") from None
        if len(body) > _MAX_RESPONSE:
            raise ValueError("response too large")
        decoded = json.loads(body, object_pairs_hook=_unique_pairs)
        payload = _Response.model_validate(decoded)
        if (
            payload.worker_id != worker_id
            or payload.worker_identity_id != worker_identity_id
            or decoded.get("worker_identity_id") != str(worker_identity_id)
            or secrets.compare_digest(payload.credential_id, token)
        ):
            raise ValueError("identity mismatch")
        _anchor(directory, directory_path, identity)
        _file(enrollment_fd, _ENROLLMENT, directory, source)
        temp_name = f".runtime-{secrets.token_hex(16)}.tmp"
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        encoded = (
            json.dumps(
                payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("ascii")
            + b"\n"
        )
        with os.fdopen(os.dup(temp_fd), "wb") as stream:
            if stream.write(encoded) != len(encoded):
                raise OSError("short write")
            stream.flush()
            os.fsync(stream.fileno())
        created = os.fstat(temp_fd)
        _file(temp_fd, temp_name, directory, created)
        _anchor(directory, directory_path, identity)
        os.link(
            temp_name, _RUNTIME, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
        )
        os.unlink(temp_name, dir_fd=directory)
        temp_name = None
        _file(temp_fd, _RUNTIME, directory, created)
        os.fsync(directory)
        _anchor(directory, directory_path, identity)
        _file(temp_fd, _RUNTIME, directory, created)
    except EnrollmentError:
        raise
    except Exception:  # noqa: BLE001 - never expose request, response, or filesystem details
        if attempted:
            raise EnrollmentError("enrollment_recovery_required") from None
        raise EnrollmentError("enrollment_configuration_invalid") from None
    finally:
        if temp_name is not None and directory >= 0 and temp_fd >= 0:
            # Remove only our own temporary inode, never a replacement.
            with suppress(OSError):
                created = os.fstat(temp_fd)
                visible = os.stat(temp_name, dir_fd=directory, follow_symlinks=False)
                if (created.st_dev, created.st_ino) == (visible.st_dev, visible.st_ino):
                    os.unlink(temp_name, dir_fd=directory)
        for descriptor in (temp_fd, enrollment_fd, directory):
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
