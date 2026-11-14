"""Read an already-enrolled v4 credential without exchange or repair.

The credential directory must remain exclusively managed by a trusted
operator. UID/mode and descriptor checks do not exclude a same-UID writer.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from dlr.worker import enrollment_v4

_RECOVERY = "runtime-credential.recovery.json"
_LOCK_TIMEOUT_SECONDS = 1.0


class RuntimeCredentialError(Exception):
    """Stable, non-sensitive read error."""


class RuntimeCredentialLockBusy(RuntimeCredentialError):
    """The trusted credential directory is temporarily locked by an installer."""


@dataclass(frozen=True)
class RuntimeCredential:
    worker_id: int
    worker_identity_id: UUID
    credential_record_id: int
    secret: str = field(repr=False)


def _read_bounded(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = bytearray()
    while len(chunks) <= enrollment_v4._MAX_RESPONSE:
        part = os.read(fd, min(4096, enrollment_v4._MAX_RESPONSE + 1 - len(chunks)))
        if not part:
            break
        chunks.extend(part)
    if len(chunks) > enrollment_v4._MAX_RESPONSE:
        raise ValueError("credential too large")
    return bytes(chunks)


def _lock_directory(fd: int, *, exclusive: bool, timeout: float = _LOCK_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fcntl.flock(fd, flag | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeCredentialLockBusy("runtime_credential_unavailable") from None
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _decode(raw: bytes, *, worker_id: int, worker_identity_id: UUID) -> RuntimeCredential:
    value = json.loads(raw, object_pairs_hook=enrollment_v4._unique_pairs)
    payload = enrollment_v4._Response.model_validate(value)
    if (
        payload.worker_id != worker_id
        or payload.worker_identity_id != worker_identity_id
        or value["worker_identity_id"] != str(worker_identity_id)
    ):
        raise ValueError("credential identity mismatch")
    return RuntimeCredential(
        worker_id=worker_id,
        worker_identity_id=worker_identity_id,
        credential_record_id=payload.credential_record_id,
        secret=payload.credential_id,
    )


def load_runtime_credential(
    credential_directory: Path,
    *,
    worker_id: int,
    worker_identity_id: UUID,
    lock_timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> RuntimeCredential:
    """Reconfirm durable, unchanged identity-bound credentials or fail closed.

    A prior publish may have linked the file but failed its directory fsync.
    Re-sync both file and directory and revalidate identity and content before
    making the credential available. Never enroll, replace, delete or log.
    """
    if (
        type(worker_id) is not int
        or not 0 < worker_id <= 2**63 - 1
        or not isinstance(worker_identity_id, UUID)
        or worker_identity_id.int == 0
        or isinstance(lock_timeout_seconds, bool)
        or not isinstance(lock_timeout_seconds, (int, float))
        or not 0 <= lock_timeout_seconds <= _LOCK_TIMEOUT_SECONDS
    ):
        raise RuntimeCredentialError("runtime_credential_unavailable")
    directory = source_fd = -1
    try:
        path = Path(credential_directory)
        if not path.is_absolute():
            raise ValueError("invalid credential directory")
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(directory)
        identity = (info.st_dev, info.st_ino)
        _lock_directory(directory, exclusive=False, timeout=lock_timeout_seconds)
        enrollment_v4._anchor(directory, path, identity)
        source_fd = os.open(
            enrollment_v4._RUNTIME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        source = os.fstat(source_fd)
        if not 1 <= source.st_size <= enrollment_v4._MAX_RESPONSE:
            raise ValueError("credential length invalid")
        enrollment_v4._file(source_fd, enrollment_v4._RUNTIME, directory, source)
        raw = _read_bounded(source_fd)
        enrollment_v4._file(source_fd, enrollment_v4._RUNTIME, directory, source)
        payload = _decode(raw, worker_id=worker_id, worker_identity_id=worker_identity_id)
        # Reconfirm both contents and directory entry after an ambiguous publish.
        os.fsync(source_fd)
        os.fsync(directory)
        enrollment_v4._anchor(directory, path, identity)
        enrollment_v4._file(source_fd, enrollment_v4._RUNTIME, directory, source)
        if _read_bounded(source_fd) != raw:
            raise ValueError("credential content changed")
        enrollment_v4._file(source_fd, enrollment_v4._RUNTIME, directory, source)
        enrollment_v4._anchor(directory, path, identity)
        return payload
    except RuntimeCredentialLockBusy:
        raise
    except Exception:
        raise RuntimeCredentialError("runtime_credential_unavailable") from None
    finally:
        for descriptor in (source_fd, directory):
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)


def install_recovered_credential(
    credential_directory: Path, *, worker_id: int, worker_identity_id: UUID
) -> None:
    """Install an administrator-staged recovery without exchange or network access.

    A trusted operator must protect and exclusively manage the directory. A
    matching retry re-confirms durability. An older or conflicting credential
    is never installed; success does not prove Control still accepts it.
    """
    if (
        type(worker_id) is not int
        or not 0 < worker_id <= 2**63 - 1
        or not isinstance(worker_identity_id, UUID)
        or worker_identity_id.int == 0
    ):
        raise RuntimeCredentialError("runtime_credential_unavailable")
    directory = old_fd = staged_fd = temp_fd = -1
    temp_name: str | None = None
    try:
        path = Path(credential_directory)
        if not path.is_absolute():
            raise ValueError("invalid credential directory")
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        directory_stat = os.fstat(directory)
        identity = (directory_stat.st_dev, directory_stat.st_ino)
        _lock_directory(directory, exclusive=True)
        enrollment_v4._anchor(directory, path, identity)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        staged_fd = os.open(_RECOVERY, flags, dir_fd=directory)
        staged_stat = os.fstat(staged_fd)
        if not 1 <= staged_stat.st_size <= enrollment_v4._MAX_RESPONSE:
            raise ValueError("credential length invalid")
        enrollment_v4._file(staged_fd, _RECOVERY, directory, staged_stat)
        staged_raw = _read_bounded(staged_fd)
        staged_value = json.loads(staged_raw, object_pairs_hook=enrollment_v4._unique_pairs)
        staged = enrollment_v4._Response.model_validate(staged_value)
        if (
            staged.worker_id != worker_id
            or staged.worker_identity_id != worker_identity_id
            or staged_value["worker_identity_id"] != str(worker_identity_id)
        ):
            raise ValueError("credential identity mismatch")

        try:
            old_fd = os.open(enrollment_v4._RUNTIME, flags, dir_fd=directory)
        except FileNotFoundError:
            old = old_stat = old_raw = None
        else:
            old_stat = os.fstat(old_fd)
            if not 1 <= old_stat.st_size <= enrollment_v4._MAX_RESPONSE:
                raise ValueError("credential length invalid")
            enrollment_v4._file(old_fd, enrollment_v4._RUNTIME, directory, old_stat)
            old_raw = _read_bounded(old_fd)
            old = _decode(old_raw, worker_id=worker_id, worker_identity_id=worker_identity_id)

        if old is not None and staged.credential_record_id == old.credential_record_id:
            if not secrets.compare_digest(staged.credential_id, old.secret):
                raise ValueError("credential recovery conflicts")
            os.fsync(old_fd)
            os.fsync(staged_fd)
            os.fsync(directory)
            enrollment_v4._anchor(directory, path, identity)
            assert old_stat is not None and old_raw is not None
            enrollment_v4._file(old_fd, enrollment_v4._RUNTIME, directory, old_stat)
            enrollment_v4._file(staged_fd, _RECOVERY, directory, staged_stat)
            if _read_bounded(old_fd) != old_raw or _read_bounded(staged_fd) != staged_raw:
                raise ValueError("credential content changed")
            enrollment_v4._file(old_fd, enrollment_v4._RUNTIME, directory, old_stat)
            enrollment_v4._file(staged_fd, _RECOVERY, directory, staged_stat)
            enrollment_v4._anchor(directory, path, identity)
            return
        if old is not None and (
            staged.credential_record_id <= old.credential_record_id
            or secrets.compare_digest(staged.credential_id, old.secret)
        ):
            raise ValueError("credential is not a newer recovery")

        os.fsync(staged_fd)
        canonical = enrollment_v4._Response(
            worker_id=staged.worker_id,
            worker_identity_id=staged.worker_identity_id,
            credential_id=staged.credential_id,
            credential_record_id=staged.credential_record_id,
        )
        encoded = (
            json.dumps(canonical.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
        temp_name = f".runtime-recovery-{secrets.token_hex(16)}.tmp"
        temp_fd = os.open(
            temp_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        offset = 0
        while offset < len(encoded):
            written = os.write(temp_fd, encoded[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(temp_fd)
        temp_stat = os.fstat(temp_fd)
        enrollment_v4._file(temp_fd, temp_name, directory, temp_stat)
        enrollment_v4._file(staged_fd, _RECOVERY, directory, staged_stat)
        enrollment_v4._anchor(directory, path, identity)
        if _read_bounded(temp_fd) != encoded or _read_bounded(staged_fd) != staged_raw:
            raise ValueError("credential content changed")
        if old is None:
            # Enrollment does not share this lock; never overwrite an entry
            # which appeared while an ambiguous exchange was finishing.
            os.link(
                temp_name,
                enrollment_v4._RUNTIME,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.unlink(temp_name, dir_fd=directory)
        else:
            assert old_stat is not None and old_raw is not None
            enrollment_v4._file(old_fd, enrollment_v4._RUNTIME, directory, old_stat)
            if _read_bounded(old_fd) != old_raw:
                raise ValueError("credential content changed")
            enrollment_v4._file(old_fd, enrollment_v4._RUNTIME, directory, old_stat)
            os.replace(
                temp_name, enrollment_v4._RUNTIME, src_dir_fd=directory, dst_dir_fd=directory
            )
        temp_name = None
        enrollment_v4._file(temp_fd, enrollment_v4._RUNTIME, directory, temp_stat)
        os.fsync(directory)
        enrollment_v4._anchor(directory, path, identity)
        enrollment_v4._file(temp_fd, enrollment_v4._RUNTIME, directory, temp_stat)
        enrollment_v4._file(staged_fd, _RECOVERY, directory, staged_stat)
        if _read_bounded(temp_fd) != encoded or _read_bounded(staged_fd) != staged_raw:
            raise ValueError("credential content changed")
        enrollment_v4._file(temp_fd, enrollment_v4._RUNTIME, directory, temp_stat)
        enrollment_v4._file(staged_fd, _RECOVERY, directory, staged_stat)
        enrollment_v4._anchor(directory, path, identity)
    except Exception:
        raise RuntimeCredentialError("runtime_credential_unavailable") from None
    finally:
        if temp_name is not None and directory >= 0 and temp_fd >= 0:
            with suppress(OSError):
                created = os.fstat(temp_fd)
                visible = os.stat(temp_name, dir_fd=directory, follow_symlinks=False)
                if (created.st_dev, created.st_ino) == (visible.st_dev, visible.st_ino):
                    os.unlink(temp_name, dir_fd=directory)
        for descriptor in (temp_fd, staged_fd, old_fd, directory):
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
