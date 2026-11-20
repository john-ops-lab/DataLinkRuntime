"""Bounded Control process for one proof-bearing v4 result exchange."""

from __future__ import annotations

import json
import math
import socket
import sys
from contextlib import suppress
from typing import Any
from uuid import UUID

from dlr.control.services.claim_child_v4 import (
    _clock,
    _read_exact,
    _read_frame,
    _restrict,
    _send_frame,
)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate result field")
        value[key] = item
    return value


def _constant(_value: str) -> None:
    raise ValueError("invalid result constant")


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        raise ValueError("result unavailable")
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("result unavailable")
    return parsed


def _run(
    channel: socket.socket,
    address_space_bytes: int,
    *,
    _finalize_binding: dict[str, object] | None = None,
    _stage_binding: dict[str, object] | None = None,
    _idle_deadline: float | None = None,
    _input_cutoff: float | None = None,
) -> None:
    from dlr.control.services.claim_envelope_v4 import ClaimEnvelope

    envelope = ClaimEnvelope.open(child_address_space_bytes=address_space_bytes)
    try:
        input_requests = 0
        input_artifacts: set[int] = set()
        while True:
            if _idle_deadline is not None:
                remaining_idle = _idle_deadline - _clock()
                if not math.isfinite(remaining_idle) or remaining_idle <= 0:
                    raise ValueError("result lifetime unavailable")
                channel.settimeout(remaining_idle)
            if _finalize_binding is not None:
                bound_deadline = _finalize_binding["control_deadline"]
                if not isinstance(bound_deadline, (int, float)):
                    raise ValueError("result authority unavailable")
                remaining = float(bound_deadline) - _clock()
                if remaining <= 0:
                    raise ValueError("result authority unavailable")
                channel.settimeout(remaining)
            request = _read_frame(channel)
            input_request = request.get("operation") == "input"
            if _stage_binding is not None and any(
                request.get(name) != value
                for name, value in _stage_binding.items()
                if not (input_request and name == "operation")
            ):
                raise ValueError("result binding unavailable")
            if _finalize_binding is not None and any(
                request.get(name) != value for name, value in _finalize_binding.items()
            ):
                raise ValueError("result binding unavailable")
            from dlr.common import config

            snapshot = request["settings"]
            if type(snapshot) is not dict or set(snapshot) != set(config.Settings.model_fields):
                raise ValueError("result configuration unavailable")
            aliases: dict[str, Any] = {}
            for name, item in config.Settings.model_fields.items():
                alias = item.validation_alias or name
                if type(alias) is not str:
                    raise ValueError("result configuration unavailable")
                aliases[alias] = snapshot[name]
            config.settings = config.Settings(**aliases)
            del snapshot, aliases

            if not input_request:
                break
            from dlr.common.managed_files_v4 import MAX_FILES
            from dlr.control.services.input_child_v4 import serve

            if _stage_binding is None and _finalize_binding is None and _idle_deadline is None:
                deadline = request.get("control_deadline")
                if (
                    config.settings.control_ha_slot is None
                    or request.get("body_bytes") != 0
                    or isinstance(deadline, bool)
                    or not isinstance(deadline, (int, float))
                    or not math.isfinite(deadline)
                    or deadline <= _clock()
                ):
                    raise ValueError("input unavailable")
                _send_frame(channel, {"state": "ready"})
                serve(channel, request, envelope, idle_until=deadline)
                return
            if _stage_binding is None or _finalize_binding is not None or _idle_deadline is None:
                raise ValueError("input unavailable")
            if (
                type(_input_cutoff) is not float
                or not math.isfinite(_input_cutoff)
                or _clock() >= _input_cutoff
            ):
                _send_frame(channel, {"state": "input_rejected"})
                continue
            supplied = request.get("control_deadline")
            if (
                not isinstance(supplied, (int, float))
                or isinstance(supplied, bool)
                or not math.isfinite(supplied)
            ):
                raise ValueError("input unavailable")
            request["control_deadline"] = min(float(supplied), _input_cutoff)
            artifact = request.get("artifact_id")
            if (
                input_requests >= MAX_FILES
                or type(artifact) is not int
                or artifact in input_artifacts
            ):
                _send_frame(channel, {"state": "input_rejected"})
                continue
            input_requests += 1
            input_artifacts.add(artifact)
            serve(channel, request, envelope, idle_until=_idle_deadline)

        from dlr.control import db
        from dlr.control.schemas.reliable_runtime import AttemptResultBody
        from dlr.control.services.attempt_result_finalize_v4 import (
            ResultControlFence,
            ResultFinalizeRejected,
            ResultFinalizeUnknown,
            _control_authority,
            finalize_unconfirmed_result_from_token,
            observe_committed_result_from_token,
        )
        from dlr.control.services.attempt_result_intent_v4 import (
            ResultIntentContext,
            ResultIntentRef,
            observe_result_in_session,
            stage_result_intent,
        )
        from dlr.control.services.control_declaration_v4 import read_declaration
        from dlr.control.services.worker_lease_candidates_v4 import ProofAuthority

        deadline = request["control_deadline"]
        size = request["body_bytes"]
        operation = request["operation"]
        if (
            type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or deadline <= _clock()
            or type(size) is not int
            or not 0 < size <= address_space_bytes
            or operation not in {"stage", "finalize"}
            or type(request["worker_id"]) is not int
            or not 0 < request["worker_id"] < 2**63
            or type(request["attempt_id"]) is not int
            or not 0 < request["attempt_id"] < 2**63
            or type(request["generation"]) is not int
            or not 0 < request["generation"] < 2**63
            or type(request["credential"]) is not str
            or type(request["instance_proof"]) is not str
            or type(request["attempt_proof"]) is not str
        ):
            raise ValueError("result unavailable")
        declaration = read_declaration(config.settings)

        def local_check() -> None:
            if _clock() >= deadline or read_declaration(config.settings) != declaration:
                raise ValueError("result authority unavailable")
            envelope.check()

        control = ResultControlFence(
            declaration,
            _uuid(request["control_instance"]),
            request["control_token"],
            local_check,
        )
        instance = _uuid(request["instance_id"])
        observation_nonce = _uuid(request["observation_nonce"])
        local_check()
        _send_frame(channel, {"state": "ready"})
        raw = _read_exact(channel, size)
        action = AttemptResultBody.model_validate(
            json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        )
        del raw
        if action.attempt_id != request["attempt_id"]:
            raise ValueError("result unavailable")
        local_check()

        def context_for(authority: ProofAuthority) -> ResultIntentContext:
            return ResultIntentContext(
                request["worker_id"],
                request["attempt_id"],
                request["credential"],
                instance,
                request["generation"],
                request["instance_proof"],
                request["attempt_proof"],
                action,
                authority,
            )

        if operation == "stage":
            with db.SessionLocal() as session:
                gate, authority = _control_authority(session, control)
                context = context_for(authority)
                ref = stage_result_intent(session, context, max_body_bytes=address_space_bytes)
                gate.check()
                session.commit()
                local_check()
        else:
            # Decode the already-signed intent using the trusted local key.
            # This grants no DB authority: finalization and receipt observation
            # each replace it under their own fresh transaction fence before
            # checking ownership or changing/confirming any responsibility.
            if declaration.worker_proof_signing_key is None:
                raise ValueError("result authority unavailable")
            context = context_for(
                ProofAuthority(
                    declaration.authority.cluster_id,
                    declaration.authority_epoch,
                    declaration.worker_proof_signing_key,
                )
            )
            ref = ResultIntentRef(request["attempt_id"], _uuid(request["intent_id"]))
        if operation == "stage":
            with db.SessionLocal() as session:
                gate, authority = _control_authority(session, control)
                context = ResultIntentContext(
                    context.worker_id,
                    context.attempt_id,
                    context.credential,
                    context.instance_id,
                    context.generation,
                    context.instance_proof,
                    context.attempt_proof,
                    action,
                    authority,
                )
                proof = observe_result_in_session(
                    session,
                    context,
                    ref,
                    max_body_bytes=address_space_bytes,
                    observation_nonce=observation_nonce,
                )
                gate.check()
                session.rollback()
            if proof.token is None:
                raise ValueError("result proof unavailable")
            local_check()
            _send_frame(channel, {"intent_id": str(ref.intent_id), "result_proof": proof.token})
            # Exactly one follow-up for this signed intent, in the same finite
            # envelope and before the original deadline. Never retain business
            # data or reuse the interpreter for another task or another stage.
            binding = {
                **request,
                "operation": "finalize",
                "intent_id": str(ref.intent_id),
                "result_proof": proof.token,
            }
            del action, context, context_for, proof, ref, request
            _run(channel, address_space_bytes, _finalize_binding=binding)
            return
        result_proof = request["result_proof"]
        receipt_nonce = _uuid(request["receipt_nonce"])
        if type(result_proof) is not str or receipt_nonce == observation_nonce:
            raise ValueError("result proof unavailable")
        # A retry may find the exact finalization already committed. Only the
        # independent receipt observer below can distinguish that from failure.
        with suppress(ResultFinalizeUnknown, ResultFinalizeRejected):
            finalize_unconfirmed_result_from_token(
                db.SessionLocal,
                context,
                ref,
                result_proof,
                observation_nonce=observation_nonce,
                control=control,
                max_body_bytes=address_space_bytes,
            )
        receipt = observe_committed_result_from_token(
            db.SessionLocal,
            context,
            ref,
            result_proof,
            observation_nonce=observation_nonce,
            receipt_nonce=receipt_nonce,
            control=control,
            max_body_bytes=address_space_bytes,
        )
        if receipt.token is None:
            raise ValueError("result receipt unavailable")
        local_check()
        _send_frame(channel, {"result_receipt": receipt.token})
    finally:
        envelope.close()


def main() -> int:
    try:
        if sys.platform != "linux" or len(sys.argv) != 4:
            raise ValueError("result process unavailable")
        fd, parent_pid, address_space_bytes = (int(value) for value in sys.argv[1:])
        _restrict(parent_pid, address_space_bytes)
        with socket.socket(fileno=fd) as channel:
            channel.sendall(b"R")
            _run(channel, address_space_bytes)
        return 0
    except BaseException:
        with suppress(Exception):
            socket.socket(fileno=int(sys.argv[1])).close()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
