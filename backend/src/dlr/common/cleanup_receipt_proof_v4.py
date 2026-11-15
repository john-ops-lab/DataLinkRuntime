"""Signed committed-cleanup observation; local physical proof remains separate."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dlr.common.cleanup_intent_proof_v4 import verify_cleanup_intent_proof

CONTEXT = b"dlr/cleanup-receipt/observation/v1\x00"
VERSION = 1
WIRE = struct.Struct(">B32s16s16sq")
TOKEN_LENGTH = ((WIRE.size + 64 + 2) // 3) * 4


@dataclass(frozen=True)
class CleanupReceiptClaims:
    completion_id: UUID
    observation_nonce: UUID
    observed_us: int


def verify_cleanup_receipt_proof(
    token: object,
    *,
    public_key: object,
    instance_token: object,
    intent_token: object,
    observation_nonce: UUID,
) -> CleanupReceiptClaims | None:
    """Check an exact intent and nonce; the caller still verifies local timing."""
    intent = verify_cleanup_intent_proof(
        intent_token, public_key=public_key, instance_token=instance_token
    )
    if (
        intent is None
        or type(public_key) is not bytes
        or type(intent_token) is not str
        or type(observation_nonce) is not UUID
        or observation_nonce.int == 0
    ):
        return None
    try:
        if type(token) is not str or len(token) != TOKEN_LENGTH:
            return None
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != WIRE.size + 64 or base64.urlsafe_b64encode(raw) != encoded:
            return None
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw[WIRE.size :], CONTEXT + raw[: WIRE.size]
        )
        version, digest, completion, nonce, observed = WIRE.unpack(raw[: WIRE.size])
        completion_id, received_nonce = UUID(bytes=completion), UUID(bytes=nonce)
        if (
            version != VERSION
            or completion_id.int == 0
            or received_nonce != observation_nonce
            or not hmac.compare_digest(
                digest, hashlib.sha256(intent_token.encode("ascii")).digest()
            )
            or not intent.observed_us <= observed < intent.proof_before_us
        ):
            return None
        return CleanupReceiptClaims(completion_id, received_nonce, observed)
    except (
        binascii.Error,
        InvalidSignature,
        OverflowError,
        TypeError,
        UnicodeError,
        UnsupportedAlgorithm,
        ValueError,
        struct.error,
    ):
        return None
