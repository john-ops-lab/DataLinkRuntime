"""Unwired instance-lease proof codec; never an authorization gate by itself.

The issuer must obtain an exact candidate in a fresh transaction after its
commit, using the authoritative database clock before the previous effective
deadline. The verifier must use the exact current candidate and a fresh DB
clock sampled under the prescribed locks, and separately authenticate the
Worker credential. This module cannot establish those preconditions, key
distribution, clock health, physical stop, or database failover durability.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dlr.common.lease_proof_wire_v4 import (
    PROOF_BYTES as _TOKEN_BYTES,
)
from dlr.common.lease_proof_wire_v4 import (
    PROOF_CONTEXT as _CONTEXT,
)
from dlr.common.lease_proof_wire_v4 import (
    PROOF_STRUCT as _STRUCT,
)
from dlr.common.lease_proof_wire_v4 import (
    PROOF_TEXT_LENGTH as _TOKEN_TEXT_LENGTH,
)
from dlr.common.lease_proof_wire_v4 import (
    PROOF_VERSION as _VERSION,
)
from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class LeaseProofError(ValueError):
    """A non-sensitive rejection of malformed proof input."""


@dataclass(frozen=True)
class LeaseProofBinding:
    cluster_id: UUID
    authority_epoch: UUID
    worker_id: int
    identity_id: UUID
    instance_id: UUID
    generation: int
    revision: int
    candidate_id: UUID
    nominal_until: datetime
    guard_until: datetime
    proof_before: datetime


@dataclass(frozen=True)
class IssuedObservationProof:
    token: str = field(repr=False)


def _key(key: bytes) -> None:
    # Format validation is not proof of entropy or identical cluster config.
    if type(key) is not bytes or len(key) != 32:
        raise LeaseProofError("lease proof unavailable")


def public_key_bytes(key: bytes) -> bytes:
    """Derive the public verifier without distributing the signing key."""
    try:
        _key(key)
        return Ed25519PrivateKey.from_private_bytes(key).public_key().public_bytes_raw()
    except (ValueError, UnsupportedAlgorithm):
        raise LeaseProofError("lease proof unavailable") from None


def _timestamp(value: datetime) -> int:
    if type(value) is not datetime:
        raise LeaseProofError("lease proof unavailable")
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            raise LeaseProofError("lease proof unavailable")
        delta = value.astimezone(UTC) - _EPOCH
        return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    except (OverflowError, TypeError, ValueError):
        raise LeaseProofError("lease proof unavailable") from None


def _pack(binding: LeaseProofBinding, observed_us: int) -> bytes:
    if type(binding) is not LeaseProofBinding or type(observed_us) is not int:
        raise LeaseProofError("lease proof unavailable")
    ids = (
        binding.cluster_id,
        binding.authority_epoch,
        binding.identity_id,
        binding.instance_id,
        binding.candidate_id,
    )
    if any(type(value) is not UUID or value.int == 0 for value in ids):
        raise LeaseProofError("lease proof unavailable")
    if any(
        type(value) is not int or not 0 < value < 2**63
        for value in (binding.worker_id, binding.generation, binding.revision)
    ):
        raise LeaseProofError("lease proof unavailable")
    nominal = _timestamp(binding.nominal_until)
    guard = _timestamp(binding.guard_until)
    previous = _timestamp(binding.proof_before)
    if (
        observed_us >= min(nominal, guard, previous)
        or previous > guard
        or max(nominal, guard) > observed_us + INSTANCE_LEASE_SECONDS * 1_000_000
    ):
        raise LeaseProofError("lease proof unavailable")
    try:
        return _STRUCT.pack(
            _VERSION,
            binding.cluster_id.bytes,
            binding.authority_epoch.bytes,
            binding.worker_id,
            binding.identity_id.bytes,
            binding.instance_id.bytes,
            binding.generation,
            binding.revision,
            binding.candidate_id.bytes,
            nominal,
            guard,
            previous,
            observed_us,
        )
    except (OverflowError, struct.error):
        raise LeaseProofError("lease proof unavailable") from None


def issue_observation_proof(
    *, key: bytes, binding: LeaseProofBinding, observed_at: datetime
) -> IssuedObservationProof:
    """Sign caller-provided facts; the caller must prove their DB origin."""
    _key(key)
    payload = _pack(binding, _timestamp(observed_at))
    try:
        signature = Ed25519PrivateKey.from_private_bytes(key).sign(_CONTEXT + payload)
    except (ValueError, UnsupportedAlgorithm):
        raise LeaseProofError("lease proof unavailable") from None
    return IssuedObservationProof(base64.urlsafe_b64encode(payload + signature).decode("ascii"))


def verify_observation_proof(
    *, token: str, key: bytes, binding: LeaseProofBinding, database_now: datetime
) -> bool:
    """Compare a proof with caller-supplied locked facts, not a DB authority."""
    try:
        _key(key)
        if type(token) is not str or len(token) != _TOKEN_TEXT_LENGTH:
            return False
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != _TOKEN_BYTES or base64.urlsafe_b64encode(raw) != encoded:
            return False
        payload, signature = raw[: _STRUCT.size], raw[_STRUCT.size :]
        Ed25519PrivateKey.from_private_bytes(key).public_key().verify(signature, _CONTEXT + payload)
        values = _STRUCT.unpack(payload)
        observed_us = cast(int, values[-1])
        if not hmac.compare_digest(payload, _pack(binding, observed_us)):
            return False
        now = _timestamp(database_now)
        return (
            observed_us
            <= now
            < min(_timestamp(binding.nominal_until), _timestamp(binding.guard_until))
        )
    except (
        LeaseProofError,
        InvalidSignature,
        UnsupportedAlgorithm,
        binascii.Error,
        OverflowError,
        TypeError,
        ValueError,
        struct.error,
    ):
        return False
