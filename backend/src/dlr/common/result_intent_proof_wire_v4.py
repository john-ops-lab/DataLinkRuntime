"""Public wire format for an observed v4 result intent, not result authority."""

from __future__ import annotations

import base64
import binascii
import hmac
import struct
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dlr.common.attempt_lease_proof_wire_v4 import verify_attempt_proof_signature
from dlr.common.lease_proof_wire_v4 import verify_worker_proof_signature
from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS

RESULT_INTENT_PROOF_VERSION = 2
RESULT_INTENT_PROOF_STRUCT = struct.Struct(">B16s16sQ16s16sQQQQ16sQ16s16s16s32sqq")
RESULT_INTENT_PROOF_CONTEXT = b"dlr/result-intent/observation/v2\x00"
RESULT_INTENT_PROOF_SIGNATURE_BYTES = 64
RESULT_INTENT_PROOF_BYTES = RESULT_INTENT_PROOF_STRUCT.size + RESULT_INTENT_PROOF_SIGNATURE_BYTES
RESULT_INTENT_PROOF_TOKEN_LENGTH = ((RESULT_INTENT_PROOF_BYTES + 2) // 3) * 4
RESULT_INTENT_PROOF_HEADER = "X-DLR-Experimental-Result-Proof"
RESULT_INTENT_PROOF_HEADER_WIRE_BYTES = (
    len(RESULT_INTENT_PROOF_HEADER.encode("ascii")) + 2 + RESULT_INTENT_PROOF_TOKEN_LENGTH + 2
)


@dataclass(frozen=True)
class UntrustedResultIntentProof:
    cluster_id: UUID
    authority_epoch: UUID
    worker_id: int
    worker_identity_id: UUID
    instance_id: UUID
    generation: int
    attempt_id: int
    fencing_token: int
    attempt_revision: int
    attempt_candidate_id: UUID
    instance_revision: int
    instance_candidate_id: UUID
    intent_id: UUID
    observation_nonce: UUID
    body_mac: bytes = field(repr=False)
    proof_before_us: int
    observed_us: int


def parse_untrusted_result_intent_proof(token: object) -> UntrustedResultIntentProof | None:
    """Parse canonical public fields only; this does not verify the signature."""
    try:
        if type(token) is not str or len(token) != RESULT_INTENT_PROOF_TOKEN_LENGTH:
            return None
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != RESULT_INTENT_PROOF_BYTES or base64.urlsafe_b64encode(raw) != encoded:
            return None
        (
            version,
            cluster,
            epoch,
            worker,
            identity,
            instance,
            generation,
            attempt,
            fence,
            attempt_revision,
            attempt_candidate,
            instance_revision,
            instance_candidate,
            intent,
            observation_nonce,
            body_mac,
            before,
            observed,
        ) = RESULT_INTENT_PROOF_STRUCT.unpack(raw[: RESULT_INTENT_PROOF_STRUCT.size])
        claims = UntrustedResultIntentProof(
            UUID(bytes=cluster),
            UUID(bytes=epoch),
            worker,
            UUID(bytes=identity),
            UUID(bytes=instance),
            generation,
            attempt,
            fence,
            attempt_revision,
            UUID(bytes=attempt_candidate),
            instance_revision,
            UUID(bytes=instance_candidate),
            UUID(bytes=intent),
            UUID(bytes=observation_nonce),
            body_mac,
            before,
            observed,
        )
        if (
            version != RESULT_INTENT_PROOF_VERSION
            or any(
                item.int == 0
                for item in (
                    claims.cluster_id,
                    claims.authority_epoch,
                    claims.worker_identity_id,
                    claims.instance_id,
                    claims.attempt_candidate_id,
                    claims.instance_candidate_id,
                    claims.intent_id,
                    claims.observation_nonce,
                )
            )
            or any(
                not 0 < item < 2**63
                for item in (
                    worker,
                    generation,
                    attempt,
                    fence,
                    attempt_revision,
                    instance_revision,
                )
            )
            or not 0 < before - observed <= INSTANCE_LEASE_SECONDS * 1_000_000
        ):
            return None
        return claims
    except (binascii.Error, OverflowError, TypeError, ValueError, struct.error):
        return None


def verify_result_intent_proof_signature(
    token: object, public_key: object
) -> UntrustedResultIntentProof | None:
    """Verify a pinned key; current ownership and result checks remain separate."""
    claims = parse_untrusted_result_intent_proof(token)
    if claims is None or type(public_key) is not bytes or len(public_key) != 32:
        return None
    try:
        assert isinstance(token, str)
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw[RESULT_INTENT_PROOF_STRUCT.size :],
            RESULT_INTENT_PROOF_CONTEXT + raw[: RESULT_INTENT_PROOF_STRUCT.size],
        )
        return claims
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError, binascii.Error):
        return None


def verify_result_intent_binding(
    token: object,
    *,
    public_key: object,
    instance_token: object,
    attempt_token: object,
    cluster_id: UUID,
    authority_epoch: UUID,
    worker_id: int,
    worker_identity_id: UUID,
    instance_id: UUID,
    generation: int,
    attempt_id: int,
    fencing_token: int,
    intent_id: UUID,
    observation_nonce: UUID,
    body_mac: bytes,
) -> UntrustedResultIntentProof | None:
    """Compare three signatures and exact bindings, but not local or DB liveness."""
    if (
        any(
            type(value) is not UUID or value.int == 0
            for value in (
                cluster_id,
                authority_epoch,
                worker_identity_id,
                instance_id,
                intent_id,
                observation_nonce,
            )
        )
        or any(
            type(value) is not int or not 0 < value < 2**63
            for value in (worker_id, generation, attempt_id, fencing_token)
        )
        or type(body_mac) is not bytes
        or len(body_mac) != 32
    ):
        return None
    result = verify_result_intent_proof_signature(token, public_key)
    instance = verify_worker_proof_signature(instance_token, public_key)
    attempt = verify_attempt_proof_signature(attempt_token, public_key)
    if result is None or instance is None or attempt is None:
        return None
    if (
        result.cluster_id != cluster_id
        or instance.cluster_id != cluster_id
        or attempt.cluster_id != cluster_id
        or result.authority_epoch != authority_epoch
        or instance.authority_epoch != authority_epoch
        or attempt.authority_epoch != authority_epoch
        or result.worker_id != worker_id
        or instance.worker_id != worker_id
        or attempt.worker_id != worker_id
        or result.worker_identity_id != worker_identity_id
        or instance.worker_identity_id != worker_identity_id
        or attempt.worker_identity_id != worker_identity_id
        or result.instance_id != instance_id
        or instance.instance_id != instance_id
        or attempt.instance_id != instance_id
        or result.generation != generation
        or instance.generation != generation
        or attempt.generation != generation
        or result.attempt_id != attempt_id
        or attempt.attempt_id != attempt_id
        or result.fencing_token != fencing_token
        or attempt.fencing_token != fencing_token
        or result.intent_id != intent_id
        or result.observation_nonce != observation_nonce
        or result.attempt_revision != attempt.revision
        or result.attempt_candidate_id != attempt.candidate_id
        or result.instance_revision != instance.revision
        or result.instance_candidate_id != instance.candidate_id
        or not hmac.compare_digest(result.body_mac, body_mac)
        or result.observed_us < max(instance.observed_us, attempt.observed_us)
        or result.proof_before_us
        > min(instance.nominal_us, instance.guard_us, attempt.nominal_us, attempt.guard_us)
    ):
        return None
    return result
