"""Supervised Control process for one two-phase fixed Worker Claim.

The parent supplies its private authority over a socket. This child must never
read a business payload until it is inside the limited Control cgroup and its
own address-space limit is installed. It does not issue execution proofs.
"""

from __future__ import annotations

import ctypes
import gc
import json
import math
import os
import resource
import signal
import socket
import struct
import sys
import time
from contextlib import suppress
from typing import Any
from uuid import UUID

_MAX_FRAME = 131_072
_CHUNK = 65_536


def _read_exact(channel: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = channel.recv(min(_CHUNK, size - len(result)))
        if not chunk:
            raise ValueError("Claim parent unavailable")
        result.extend(chunk)
    return bytes(result)


def _read_frame(channel: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _read_exact(channel, 4))[0]
    if not 0 < size <= _MAX_FRAME:
        raise ValueError("Claim frame unavailable")
    value = json.loads(_read_exact(channel, size))
    if type(value) is not dict:
        raise ValueError("Claim frame unavailable")
    return value


def _send_frame(channel: socket.socket, value: dict[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if not 0 < len(raw) <= _MAX_FRAME:
        raise ValueError("Claim frame unavailable")
    channel.sendall(struct.pack("!I", len(raw)) + raw)


def _clock() -> float:
    value = time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined, unused-ignore]
    if not math.isfinite(value) or value < 0:
        raise ValueError("Claim deadline unavailable")
    return value


def _restrict(parent_pid: int, address_space_bytes: int) -> None:
    if (
        sys.platform != "linux"
        or type(parent_pid) is not int
        or parent_pid < 1
        or type(address_space_bytes) is not int
        or not 128 * 1024 * 1024 <= address_space_bytes <= 1 << 40
    ):
        raise ValueError("Claim process unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    if (
        libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0  # PR_SET_PDEATHSIG
        or os.getppid() != parent_pid
        or libc.prctl(4, 0, 0, 0, 0) != 0  # PR_SET_DUMPABLE
    ):
        raise ValueError("Claim process unavailable")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (address_space_bytes, address_space_bytes))


def _run(channel: socket.socket, address_space_bytes: int) -> dict[str, object] | None:
    from dlr.control.services.claim_envelope_v4 import ClaimEnvelope

    envelope = ClaimEnvelope.open(child_address_space_bytes=address_space_bytes)
    try:
        request = _read_frame(channel)
        from dlr.common import config

        snapshot = request["settings"]
        if type(snapshot) is not dict or set(snapshot) != set(config.Settings.model_fields):
            raise ValueError("Claim configuration unavailable")
        aliases: dict[str, Any] = {}
        for name, field in config.Settings.model_fields.items():
            alias = field.validation_alias or name
            if type(alias) is not str:
                raise ValueError("Claim configuration unavailable")
            aliases[alias] = snapshot[name]
        config.settings = config.Settings(**aliases)

        # Load these only after replacing the child-local settings instance.
        from dlr.control import db
        from dlr.control.schemas.reliable_runtime import AttemptActionBody
        from dlr.control.services import worker_candidate_storage_preflight_v4, worker_claim_v4
        from dlr.control.services import worker_proof_authority_v4 as proof_authority
        from dlr.control.services.attempt import V4AttemptAuthority
        from dlr.control.services.control_declaration_v4 import read_declaration
        from dlr.control.services.dispatch import deserialize_dispatch_message
        from dlr.control.services.worker_token_fence_v4 import acquire

        deadline = request["control_deadline"]
        if (
            type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or deadline <= _clock()
        ):
            raise ValueError("Claim authority unavailable")
        control_instance = UUID(request["control_instance"])
        control_token = request["control_token"]
        worker_id = request["worker_id"]
        instance = UUID(request["instance_id"])
        generation = request["generation"]
        credential = request["credential"]
        instance_proof = request["instance_proof"]
        message = deserialize_dispatch_message(request["dispatch"])
        budget = worker_claim_v4.ClaimWireBudget(request["wire_budget"])
        authority = V4AttemptAuthority(credential, instance, generation)
        declaration = read_declaration(config.settings)

        def local_check() -> None:
            if _clock() >= deadline or read_declaration(config.settings) != declaration:
                raise ValueError("Claim authority unavailable")
            envelope.check()

        def get_fence(session: Any) -> Any:
            local_check()
            fence = acquire(
                session,
                declaration.authority,
                instance_id=control_instance,
                instance_token=control_token,
                mode="operation",
            )
            local_check()
            return fence

        def get_proof_authority(session: Any) -> Any:
            local_check()
            if (
                worker_candidate_storage_preflight_v4.inspect_candidate_storage(
                    session, expected_schema="public"
                ).state
                != "observed"
            ):
                raise ValueError("Claim authority unavailable")
            return proof_authority.for_transaction(session, declaration)

        with db.SessionLocal() as session:
            fence = get_fence(session)
            proof = get_proof_authority(session)

            def check_preparation() -> None:
                local_check()
                fence.check()
                local_check()

            decision, proposal = worker_claim_v4.prepare_claim(
                session,
                worker_id,
                message,
                authority,
                wire_budget=budget,
                proof_context=worker_claim_v4.ClaimProofContext(proof, instance_proof),
                check_authority=check_preparation,
            )
        wire = decision._wire_bytes or worker_claim_v4._serialize_claim(decision)
        if len(wire) > budget.max_response_bytes:
            raise ValueError("Claim response unavailable")
        local_check()
        _send_frame(channel, {"decision": decision.decision, "size": len(wire)})
        channel.sendall(wire)
        if proposal is None:
            return None
        # Do not keep the business snapshot while waiting for confirmation.
        del wire, decision
        remaining = deadline - _clock()
        if remaining <= 0:
            raise ValueError("Claim authority unavailable")
        channel.settimeout(remaining)
        command = _read_frame(channel)
        if set(command) != {"op", "instance_proof"} or command["op"] != "finalize":
            raise ValueError("Claim confirmation unavailable")
        final_proof = command["instance_proof"]
        if type(final_proof) is not str:
            raise ValueError("Claim confirmation unavailable")
        local_check()
        with db.SessionLocal() as session:
            fence = get_fence(session)
            proof = get_proof_authority(session)
            result = worker_claim_v4.finalize_claim(
                session,
                worker_id,
                message,
                authority,
                wire_budget=budget,
                proof_context=worker_claim_v4.ClaimProofContext(proof, final_proof),
                proposal=proposal,
            )
            if result.decision != "EXECUTE":
                session.rollback()
                _send_frame(channel, {"state": "not_committed", "reason": result.reason})
                return None
            candidate, payload = result._attempt_candidate, result.payload
            if candidate is None or payload is None:
                raise ValueError("Claim confirmation unavailable")
            execution_timeout = payload.execution_timeout_seconds
            if type(execution_timeout) is not int or not 0 < execution_timeout <= 86_400:
                raise ValueError("Claim execution timeout unavailable")
            action = AttemptActionBody(
                attempt_id=payload.attempt_id,
                fencing_token=payload.fencing_token,
                claim_token=payload.claim_token,
            )
            local_check()
            fence.check()
            local_check()
            session.commit()
            local_check()
        idle_until = math.nextafter(_clock() + execution_timeout + 30.0, -math.inf)
        _send_frame(
            channel,
            {
                "state": "committed",
                "candidate": {
                    "worker_id": candidate.worker_id,
                    "attempt_id": candidate.attempt_id,
                    "instance_id": str(candidate.instance_id),
                    "generation": candidate.generation,
                    "fencing_token": candidate.fencing_token,
                    "revision": candidate.revision,
                    "candidate_id": str(candidate.candidate_id),
                },
                "action": action.model_dump(mode="json"),
                "execution_timeout_seconds": execution_timeout,
                "result_idle_until": idle_until,
            },
        )
        # Returning destroys this Claim frame and its business snapshot before
        # the same bounded interpreter waits for the matching result request.
        return {
            "operation": "stage",
            "worker_id": worker_id,
            "attempt_id": candidate.attempt_id,
            "credential": credential,
            "instance_id": str(instance),
            "generation": generation,
            "settings": request["settings"],
            "control_instance": request["control_instance"],
            "control_token": request["control_token"],
            "result_idle_until": idle_until,
        }
    finally:
        envelope.close()


def main() -> int:
    try:
        if len(sys.argv) != 4:
            raise ValueError
        fd, parent_pid, address_space_bytes = (int(value) for value in sys.argv[1:])
        _restrict(parent_pid, address_space_bytes)
        with socket.socket(fileno=fd) as channel:
            # The creator thread must still be alive after this handshake;
            # PR_SET_PDEATHSIG cannot detect that it died before installation.
            channel.sendall(b"R")
            binding = _run(channel, address_space_bytes)
            if binding is not None:
                gc.collect()
                from dlr.control.services.result_child_v4 import _run as result_exchange

                idle_until = binding.pop("result_idle_until")
                if not isinstance(idle_until, (int, float)):
                    raise ValueError("result lifetime unavailable")
                result_exchange(
                    channel,
                    address_space_bytes,
                    _stage_binding=binding,
                    _idle_deadline=float(idle_until),
                )
        return 0
    except BaseException:
        # No exception text, SQL, paths, credentials, or payload reaches logs.
        with suppress(Exception):
            socket.socket(fileno=int(sys.argv[1])).close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
