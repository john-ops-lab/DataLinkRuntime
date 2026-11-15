"""Signed cleanup-intent observation; never proof of physical cleanup or ACK."""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dlr.common.lease_proof_wire_v4 import verify_worker_proof_signature
from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS

CONTEXT = b"dlr/cleanup-intent/observation/v1\x00"
VERSION = 1
WIRE = struct.Struct(">B16s16sQ16sQQ16sQ16sQQQ16s16s16sqq")
TOKEN_LENGTH = ((WIRE.size + 64 + 2) // 3) * 4


@dataclass(frozen=True)
class CleanupIntentClaims:
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
    intent_id: UUID
    observation_nonce: UUID
    proof_before_us: int
    observed_us: int


def _parse(token: object) -> CleanupIntentClaims | None:
    """Parse canonical fields; these claims have no authority before verification."""
    try:
        if type(token) is not str or len(token) != TOKEN_LENGTH:
            return None
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != WIRE.size + 64 or base64.urlsafe_b64encode(raw) != encoded:
            return None
        (
            version,
            cluster,
            epoch,
            worker,
            identity,
            execution,
            attempt,
            original,
            original_generation,
            current,
            generation,
            fence,
            revision,
            candidate,
            intent,
            nonce,
            before,
            observed,
        ) = WIRE.unpack(raw[: WIRE.size])
        claims = CleanupIntentClaims(
            UUID(bytes=cluster),
            UUID(bytes=epoch),
            worker,
            UUID(bytes=identity),
            execution,
            attempt,
            UUID(bytes=original),
            original_generation,
            UUID(bytes=current),
            generation,
            fence,
            revision,
            UUID(bytes=candidate),
            UUID(bytes=intent),
            UUID(bytes=nonce),
            before,
            observed,
        )
        if (
            version != VERSION
            or any(
                item.int == 0
                for item in (
                    claims.cluster_id,
                    claims.authority_epoch,
                    claims.worker_identity_id,
                    claims.original_instance_id,
                    claims.instance_id,
                    claims.instance_candidate_id,
                    claims.intent_id,
                    claims.observation_nonce,
                )
            )
            or any(
                not 0 < item < 2**63
                for item in (
                    worker,
                    execution,
                    attempt,
                    original_generation,
                    generation,
                    fence,
                    revision,
                )
            )
            or original_generation > generation
            or not 0 < before - observed <= INSTANCE_LEASE_SECONDS * 1_000_000
        ):
            return None
        return claims
    except (
        binascii.Error,
        OverflowError,
        TypeError,
        UnicodeError,
        ValueError,
        struct.error,
    ):
        return None


def verify_cleanup_intent_signature(
    token: object, public_key: object
) -> CleanupIntentClaims | None:
    """Verify the pinned Control signature; current ownership remains separate."""
    claims = _parse(token)
    if claims is None or type(public_key) is not bytes or len(public_key) != 32:
        return None
    try:
        assert isinstance(token, str)
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw[WIRE.size :], CONTEXT + raw[: WIRE.size]
        )
        return claims
    except (InvalidSignature, UnsupportedAlgorithm, binascii.Error, TypeError, ValueError):
        return None


def verify_cleanup_intent_proof(
    token: object, *, public_key: object, instance_token: object
) -> CleanupIntentClaims | None:
    """Verify both signatures and their original binding; caller checks timing and nonce."""
    claims = verify_cleanup_intent_signature(token, public_key)
    instance = verify_worker_proof_signature(instance_token, public_key)
    if (
        claims is None
        or instance is None
        or claims.cluster_id != instance.cluster_id
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
