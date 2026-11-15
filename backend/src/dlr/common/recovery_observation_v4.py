"""Current-instance observation of stable historical result/cleanup markers.

It grants no execution or physical-cleanup permission. The caller checks its
fresh nonce, exact checkpoint/intent binding and conservative local deadline.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from dlr.common.lease_proof_wire_v4 import verify_worker_proof_signature
from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS

CONTEXT = b"dlr/recovery/observation/v1\x00"
VERSION = 1
# Header, current/original ownership, maintenance+checkpoint, stable result,
# optional committed cleanup, nonce, terminal status, observation window.
WIRE = struct.Struct(">B16s16sQ16sQQ16sQ16sQQQ16s16s32s16s32s16s16s16sBqq")
TOKEN_LENGTH = ((WIRE.size + 64 + 2) // 3) * 4
STATUS_TO_CODE = {"succeeded": 1, "failed": 2, "timed_out": 3}
CODE_TO_STATUS = {value: key for key, value in STATUS_TO_CODE.items()}


@dataclass(frozen=True)
class RecoveryClaims:
    cluster_id: UUID
    authority_epoch: UUID
    worker_id: int
    worker_identity_id: UUID
    execution_id: int
    attempt_id: int
    original_instance_id: UUID
    original_generation: int
    instance_id: UUID
    generation: int
    fencing_token: int
    instance_revision: int
    instance_candidate_id: UUID
    operation_id: UUID
    prepared_sha256: bytes
    result_intent_id: UUID
    body_mac: bytes
    finalization_id: UUID
    completion_id: UUID | None
    observation_nonce: UUID
    attempt_status: str
    proof_before_us: int
    observed_us: int


def _payload(claims: RecoveryClaims) -> bytes:
    return WIRE.pack(
        VERSION,
        claims.cluster_id.bytes,
        claims.authority_epoch.bytes,
        claims.worker_id,
        claims.worker_identity_id.bytes,
        claims.execution_id,
        claims.attempt_id,
        claims.original_instance_id.bytes,
        claims.original_generation,
        claims.instance_id.bytes,
        claims.generation,
        claims.fencing_token,
        claims.instance_revision,
        claims.instance_candidate_id.bytes,
        claims.operation_id.bytes,
        claims.prepared_sha256,
        claims.result_intent_id.bytes,
        claims.body_mac,
        claims.finalization_id.bytes,
        claims.completion_id.bytes if claims.completion_id is not None else bytes(16),
        claims.observation_nonce.bytes,
        STATUS_TO_CODE[claims.attempt_status],
        claims.proof_before_us,
        claims.observed_us,
    )


def _parse(raw: bytes) -> RecoveryClaims | None:
    try:
        (
            version,
            cluster,
            epoch,
            worker,
            identity,
            execution,
            attempt,
            original,
            original_gen,
            current,
            generation,
            fence,
            revision,
            candidate,
            operation,
            prepared,
            intent,
            mac,
            finalization,
            completion,
            nonce,
            status,
            before,
            observed,
        ) = WIRE.unpack(raw)
        result = RecoveryClaims(
            UUID(bytes=cluster),
            UUID(bytes=epoch),
            worker,
            UUID(bytes=identity),
            execution,
            attempt,
            UUID(bytes=original),
            original_gen,
            UUID(bytes=current),
            generation,
            fence,
            revision,
            UUID(bytes=candidate),
            UUID(bytes=operation),
            prepared,
            UUID(bytes=intent),
            mac,
            UUID(bytes=finalization),
            UUID(bytes=completion) if any(completion) else None,
            UUID(bytes=nonce),
            CODE_TO_STATUS.get(status, ""),
            before,
            observed,
        )
        if (
            version != VERSION
            or not result.attempt_status
            or any(
                value.int == 0
                for value in (
                    result.cluster_id,
                    result.authority_epoch,
                    result.worker_identity_id,
                    result.original_instance_id,
                    result.instance_id,
                    result.instance_candidate_id,
                    result.operation_id,
                    result.result_intent_id,
                    result.finalization_id,
                    result.observation_nonce,
                )
            )
            or any(
                not 0 < value < 2**63
                for value in (worker, execution, attempt, original_gen, generation, fence, revision)
            )
            or original_gen >= generation
            or original == current
            or not 0 < before - observed <= INSTANCE_LEASE_SECONDS * 1_000_000
        ):
            return None
        return result
    except (ValueError, TypeError, OverflowError, struct.error):
        return None


def sign_observation(claims: RecoveryClaims, key: bytes) -> str:
    payload = _payload(claims)
    if _parse(payload) != claims:
        raise ValueError("recovery observation invalid")
    signature = Ed25519PrivateKey.from_private_bytes(key).sign(CONTEXT + payload)
    return base64.urlsafe_b64encode(payload + signature).decode("ascii")


def verify_observation(
    token: object, *, public_key: object, instance_token: object
) -> RecoveryClaims | None:
    instance = verify_worker_proof_signature(instance_token, public_key)
    if (
        type(token) is not str
        or len(token) != TOKEN_LENGTH
        or instance is None
        or type(public_key) is not bytes
        or len(public_key) != 32
    ):
        return None
    try:
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != WIRE.size + 64 or base64.urlsafe_b64encode(raw) != encoded:
            return None
        claims = _parse(raw[: WIRE.size])
        if claims is None:
            return None
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw[WIRE.size :], CONTEXT + raw[: WIRE.size]
        )
        if (
            claims.cluster_id != instance.cluster_id
            or claims.authority_epoch != instance.authority_epoch
            or claims.worker_id != instance.worker_id
            or claims.worker_identity_id != instance.worker_identity_id
            or claims.instance_id != instance.instance_id
            or claims.generation != instance.generation
            or claims.instance_revision != instance.revision
            or claims.instance_candidate_id != instance.candidate_id
            or claims.observed_us < instance.observed_us
            or claims.proof_before_us > min(instance.nominal_us, instance.guard_us)
        ):
            return None
        return claims
    except (
        InvalidSignature,
        UnsupportedAlgorithm,
        binascii.Error,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return None
