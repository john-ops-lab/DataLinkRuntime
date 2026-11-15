"""Signed observation of a committed result; it is not cleanup or ACK authority."""

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

from dlr.common.result_intent_proof_wire_v4 import verify_result_intent_proof_signature

RESULT_RECEIPT_CONTEXT = b"dlr/result-finalization/observation/v1\x00"
RESULT_RECEIPT_VERSION = 1
RESULT_RECEIPT_STRUCT = struct.Struct(">B32s16s16sBBq")
RESULT_RECEIPT_SIGNATURE_BYTES = 64
RESULT_RECEIPT_BYTES = RESULT_RECEIPT_STRUCT.size + RESULT_RECEIPT_SIGNATURE_BYTES
RESULT_RECEIPT_TOKEN_LENGTH = ((RESULT_RECEIPT_BYTES + 2) // 3) * 4
ATTEMPT_STATUS_CODES = {
    "succeeded": 1,
    "failed": 2,
    "timed_out": 3,
    "cancelled": 4,
    "worker_lost": 5,
    "resource_exceeded": 6,
}
EXECUTION_STATUS_CODES = {"succeeded": 1, "dead_letter": 2, "cancelled": 3, "retry_wait": 4}
_ATTEMPT_STATUSES = {code: status for status, code in ATTEMPT_STATUS_CODES.items()}
_EXECUTION_STATUSES = {code: status for status, code in EXECUTION_STATUS_CODES.items()}


@dataclass(frozen=True)
class ResultReceiptClaims:
    finalization_id: UUID
    observation_nonce: UUID
    attempt_status: str
    execution_status: str
    observed_us: int


def verify_result_receipt(
    token: object,
    *,
    public_key: object,
    intent_token: object,
    observation_nonce: UUID,
) -> ResultReceiptClaims | None:
    """Verify the receipt and exact prior intent; caller must check local timing."""
    if (
        type(public_key) is not bytes
        or len(public_key) != 32
        or type(intent_token) is not str
        or type(observation_nonce) is not UUID
        or observation_nonce.int == 0
    ):
        return None
    intent = verify_result_intent_proof_signature(intent_token, public_key)
    if intent is None:
        return None
    try:
        if type(token) is not str or len(token) != RESULT_RECEIPT_TOKEN_LENGTH:
            return None
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != RESULT_RECEIPT_BYTES or base64.urlsafe_b64encode(raw) != encoded:
            return None
        payload, signature = raw[: RESULT_RECEIPT_STRUCT.size], raw[RESULT_RECEIPT_STRUCT.size :]
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, RESULT_RECEIPT_CONTEXT + payload
        )
        version, digest, finalization, nonce, attempt, execution, observed = (
            RESULT_RECEIPT_STRUCT.unpack(payload)
        )
        finalization_id, received_nonce = UUID(bytes=finalization), UUID(bytes=nonce)
        if (
            version != RESULT_RECEIPT_VERSION
            or finalization_id.int == 0
            or received_nonce != observation_nonce
            or not hmac.compare_digest(
                digest, hashlib.sha256(intent_token.encode("ascii")).digest()
            )
            or attempt not in _ATTEMPT_STATUSES
            or execution not in _EXECUTION_STATUSES
            or not intent.observed_us <= observed < intent.proof_before_us
        ):
            return None
        return ResultReceiptClaims(
            finalization_id,
            received_nonce,
            _ATTEMPT_STATUSES[attempt],
            _EXECUTION_STATUSES[execution],
            observed,
        )
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
