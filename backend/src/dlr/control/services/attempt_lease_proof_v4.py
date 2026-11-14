"""Unwired Attempt lease proof codec; caller must establish committed facts.

An instance proof does not prove an Attempt renewal committed before its own
deadline. This codec is separate and does not authenticate any request, prove
database origin, enforce physical stop, or authorize recovery on its own.
"""

import base64
import binascii
import hmac
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dlr.common.attempt_lease_proof_wire_v4 import (
    ATTEMPT_PROOF_BYTES as _TOKEN_BYTES,
)
from dlr.common.attempt_lease_proof_wire_v4 import (
    ATTEMPT_PROOF_CONTEXT as _CONTEXT,
)
from dlr.common.attempt_lease_proof_wire_v4 import (
    ATTEMPT_PROOF_STRUCT as _STRUCT,
)
from dlr.common.attempt_lease_proof_wire_v4 import (
    ATTEMPT_PROOF_TOKEN_TEXT_LENGTH,
)
from dlr.common.attempt_lease_proof_wire_v4 import (
    ATTEMPT_PROOF_VERSION as _VERSION,
)
from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS
from dlr.control.services.worker_lease_proof_v4 import LeaseProofError, _key, _timestamp


class AttemptLeaseProofError(ValueError):
    """A non-sensitive rejection of malformed proof input."""


@dataclass(frozen=True)
class AttemptLeaseProofBinding:
    cluster_id: UUID
    authority_epoch: UUID
    worker_id: int
    identity_id: UUID
    instance_id: UUID
    generation: int
    attempt_id: int
    fencing_token: int
    revision: int
    candidate_id: UUID
    nominal_until: datetime
    guard_until: datetime
    proof_before: datetime


@dataclass(frozen=True)
class IssuedAttemptLeaseProof:
    token: str = field(repr=False)


def _pack(binding: AttemptLeaseProofBinding, observed_us: int) -> bytes:
    if type(binding) is not AttemptLeaseProofBinding or type(observed_us) is not int:
        raise AttemptLeaseProofError("Attempt lease proof unavailable")
    ids = (
        binding.cluster_id,
        binding.authority_epoch,
        binding.identity_id,
        binding.instance_id,
        binding.candidate_id,
    )
    numbers = (
        binding.worker_id,
        binding.generation,
        binding.attempt_id,
        binding.fencing_token,
        binding.revision,
    )
    if any(type(value) is not UUID or value.int == 0 for value in ids) or any(
        type(value) is not int or not 0 < value < 2**63 for value in numbers
    ):
        raise AttemptLeaseProofError("Attempt lease proof unavailable")
    try:
        nominal = _timestamp(binding.nominal_until)
        guard = _timestamp(binding.guard_until)
        previous = _timestamp(binding.proof_before)
        if (
            observed_us >= min(nominal, guard, previous)
            or previous > guard
            or max(nominal, guard) > observed_us + INSTANCE_LEASE_SECONDS * 1_000_000
        ):
            raise AttemptLeaseProofError("Attempt lease proof unavailable")
        return _STRUCT.pack(
            _VERSION,
            binding.cluster_id.bytes,
            binding.authority_epoch.bytes,
            binding.worker_id,
            binding.identity_id.bytes,
            binding.instance_id.bytes,
            binding.generation,
            binding.attempt_id,
            binding.fencing_token,
            binding.revision,
            binding.candidate_id.bytes,
            nominal,
            guard,
            previous,
            observed_us,
        )
    except (LeaseProofError, OverflowError, struct.error):
        raise AttemptLeaseProofError("Attempt lease proof unavailable") from None


def issue_attempt_lease_proof(
    *, key: bytes, binding: AttemptLeaseProofBinding, observed_at: datetime
) -> IssuedAttemptLeaseProof:
    """Sign supplied facts only after the caller has proved their DB origin."""
    try:
        _key(key)
        payload = _pack(binding, _timestamp(observed_at))
    except LeaseProofError:
        raise AttemptLeaseProofError("Attempt lease proof unavailable") from None
    try:
        signature = Ed25519PrivateKey.from_private_bytes(key).sign(_CONTEXT + payload)
    except (ValueError, UnsupportedAlgorithm):
        raise AttemptLeaseProofError("Attempt lease proof unavailable") from None
    return IssuedAttemptLeaseProof(base64.urlsafe_b64encode(payload + signature).decode("ascii"))


def verify_attempt_lease_proof(
    *, token: object, key: bytes, binding: AttemptLeaseProofBinding, database_now: datetime
) -> bool:
    """Verify against exact caller-supplied facts, not an unlocked DB snapshot."""
    try:
        _key(key)
        if type(token) is not str or len(token) != ATTEMPT_PROOF_TOKEN_TEXT_LENGTH:
            return False
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != _TOKEN_BYTES or base64.urlsafe_b64encode(raw) != encoded:
            return False
        payload, signature = raw[: _STRUCT.size], raw[_STRUCT.size :]
        Ed25519PrivateKey.from_private_bytes(key).public_key().verify(signature, _CONTEXT + payload)
        observed_us = cast(int, _STRUCT.unpack(payload)[-1])
        if not hmac.compare_digest(payload, _pack(binding, observed_us)):
            return False
        now = _timestamp(database_now)
        return (
            observed_us
            <= now
            < min(_timestamp(binding.nominal_until), _timestamp(binding.guard_until))
        )
    except (
        AttemptLeaseProofError,
        InvalidSignature,
        UnsupportedAlgorithm,
        LeaseProofError,
        binascii.Error,
        OverflowError,
        TypeError,
        ValueError,
        struct.error,
    ):
        return False
