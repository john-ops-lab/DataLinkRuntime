"""File frames served inside the already-retained Control Claim interpreter.

This module never creates a process, thread or resource slot. One read command
elicits at most one bounded block; the original read fd/lock stays here until
actual close. An uncertain close escapes to the existing retained-child path.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Any

from dlr.common import managed_files_v4 as contract
from dlr.control.services.claim_child_v4 import _clock, _read_frame, _send_frame


class _CloseUnknown(Exception):
    pass


def serve(channel: Any, request: dict[str, Any], envelope: Any, *, idle_until: float) -> None:
    from dlr.common import config
    from dlr.control import db
    from dlr.control.schemas.reliable_runtime import AttemptActionBody
    from dlr.control.services import managed_input_download_v4 as downloads
    from dlr.control.services.artifact_store import LocalFileArtifactStore
    from dlr.control.services.attempt_result_finalize_v4 import (
        ResultControlFence,
        _control_authority,
    )
    from dlr.control.services.control_declaration_v4 import read_declaration
    from dlr.control.services.result_child_v4 import _uuid

    started = _clock()
    deadline = request["control_deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= started:
        raise ValueError("input unavailable")
    deadline = min(float(deadline), started + contract.FILE_TIMEOUT_SECONDS)
    declaration = read_declaration(config.settings)

    def local_check() -> None:
        if _clock() >= deadline or read_declaration(config.settings) != declaration:
            raise ValueError("input unavailable")
        envelope.check()

    control = ResultControlFence(
        declaration, _uuid(request["control_instance"]), request["control_token"], local_check
    )
    stream = None
    ready = False
    command_pending = True

    def close_stream() -> None:
        nonlocal stream
        closing, stream = stream, None
        if closing is not None:
            try:
                closing.close()
            except BaseException:
                raise _CloseUnknown("input close unverified") from None

    try:
        action = AttemptActionBody.model_validate(
            {
                "attempt_id": request["attempt_id"],
                "fencing_token": request["fencing_token"],
                "claim_token": request["claim_token"],
            }
        )
        if any(
            type(request.get(key)) is not int or not 0 < request[key] < 2**63
            for key in ("worker_id", "execution_id", "artifact_id", "generation")
        ):
            raise ValueError("input unavailable")
        with db.SessionLocal() as session:
            gate, authority = _control_authority(session, control)
            grant, remaining = downloads.admit(
                session,
                worker_id=request["worker_id"],
                execution_id=request["execution_id"],
                artifact_id=request["artifact_id"],
                credential=request["credential"],
                instance_id=_uuid(request["instance_id"]),
                generation=request["generation"],
                instance_proof=request["instance_proof"],
                attempt_proof=request["attempt_proof"],
                action=action,
                authority=authority,
            )
            gate.check()
            session.rollback()
        deadline = min(deadline, started + remaining)

        def recheck() -> None:
            local_check()
            with db.SessionLocal() as session:
                gate, _ = _control_authority(session, control)
                downloads.recheck(session, grant)
                gate.check()
                session.rollback()
            local_check()

        recheck()
        store = LocalFileArtifactStore(config.settings.artifact_store_root)
        stream = store.open(grant.storage_key)
        before = os.fstat(stream.fileno())
        if before.st_size != grant.file.size_bytes:
            raise ValueError("input unavailable")
        digest = hashlib.sha256()
        total = 0
        while True:
            local_check()
            part = stream.read(min(contract.CHUNK_BYTES, grant.file.size_bytes - total + 1))
            if not part:
                break
            total += len(part)
            if total > grant.file.size_bytes:
                raise ValueError("input unavailable") from None
            digest.update(part)
        if total != grant.file.size_bytes or digest.hexdigest() != grant.file.sha256:
            raise ValueError("input unavailable")
        stream.seek(0)
        recheck()

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_nlink,
            )

        def finish_read() -> None:
            if (
                total != grant.file.size_bytes
                or digest.hexdigest() != grant.file.sha256
                or stream is None
                or stream.read(1) != b""
                or identity(os.fstat(stream.fileno())) != identity(before)
            ):
                raise ValueError("input unavailable")
            close_stream()

        if grant.file.size_bytes == 0:
            finish_read()
        _send_frame(
            channel,
            {
                "state": "input_ready",
                "size": grant.file.size_bytes,
                "stream_id": str(grant.stream_id),
                "control_deadline": deadline,
                **({"closed": True} if grant.file.size_bytes == 0 else {}),
            },
        )
        if grant.file.size_bytes == 0:
            return
        ready = True
        command_pending = False
        digest = hashlib.sha256()
        total = 0
        while True:
            local_check()
            channel.settimeout(max(0.001, deadline - _clock()))
            command = _read_frame(channel)
            command_pending = True
            if command == {"op": "cancel_input"}:
                close_stream()
                _send_frame(channel, {"state": "input_closed"})
                return
            if command != {"op": "read_input"}:
                raise ValueError("input unavailable")
            recheck()
            if stream is None:
                raise ValueError("input unavailable")
            part = stream.read(min(contract.CHUNK_BYTES, grant.file.size_bytes - total + 1))
            total += len(part)
            if total > grant.file.size_bytes:
                raise ValueError("input unavailable")
            if not part:
                raise ValueError("input unavailable")
            digest.update(part)
            last = total == grant.file.size_bytes
            if last:
                finish_read()
                recheck()
            local_check()
            channel.settimeout(max(0.001, deadline - _clock()))
            _send_frame(
                channel,
                {"state": "input_chunk", "bytes": len(part), **({"closed": True} if last else {})},
            )
            channel.sendall(part)
            if last:
                return
            command_pending = False
    except _CloseUnknown:
        raise
    except Exception:
        close_stream()  # Unknown physical close cannot reach the normal reply.
        # A known refusal/closed fd does not destroy the retained result
        # interpreter. The existing Attempt can report its preparation failure.
        if ready and not command_pending:
            # No unsolicited terminal frame: the next read/cancel command
            # must consume this failure before the result protocol resumes.
            # Files are already physically closed while the original finite
            # result-retention deadline bounds this diagnostic-only wait.
            channel.settimeout(max(0.001, idle_until - _clock()))
            if _read_frame(channel) not in ({"op": "read_input"}, {"op": "cancel_input"}):
                raise ValueError("input unavailable") from None
        channel.settimeout(0.5)
        _send_frame(channel, {"state": "input_failed" if ready else "input_rejected"})
    finally:
        close_stream()
