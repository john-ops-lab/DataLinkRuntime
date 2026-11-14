"""Shared wire format for experimental v4 lease proofs.

Parsing exposes untrusted claims only. Signature verification needs a
separately provisioned public key and is never full authorization by itself.
"""

import base64
import binascii
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS

PROOF_VERSION = 2
PROOF_STRUCT = struct.Struct(">B16s16sQ16s16sQQ16sqqqq")
PROOF_CONTEXT = b"dlr/worker-instance-lease-proof/v2\x00"
PROOF_SIGNATURE_BYTES = 64
PROOF_BYTES = PROOF_STRUCT.size + PROOF_SIGNATURE_BYTES
PROOF_TEXT_LENGTH = ((PROOF_BYTES + 2) // 3) * 4


@dataclass(frozen=True)
class UntrustedLeaseProof:
    cluster_id: UUID
    authority_epoch: UUID
    worker_id: int
    worker_identity_id: UUID
    instance_id: UUID
    generation: int
    revision: int
    candidate_id: UUID
    nominal_us: int
    guard_us: int
    proof_before_us: int
    observed_us: int


def parse_untrusted_proof(token: object) -> UntrustedLeaseProof | None:
    """Parse canonical public fields without checking the MAC."""
    try:
        if type(token) is not str or len(token) != PROOF_TEXT_LENGTH:
            return None
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != PROOF_BYTES or base64.urlsafe_b64encode(raw) != encoded:
            return None
        (
            version,
            cluster,
            epoch,
            worker_id,
            identity,
            instance,
            generation,
            revision,
            candidate,
            nominal,
            guard,
            previous,
            observed,
        ) = PROOF_STRUCT.unpack(raw[: PROOF_STRUCT.size])
        result = UntrustedLeaseProof(
            UUID(bytes=cluster),
            UUID(bytes=epoch),
            worker_id,
            UUID(bytes=identity),
            UUID(bytes=instance),
            generation,
            revision,
            UUID(bytes=candidate),
            nominal,
            guard,
            previous,
            observed,
        )
        if (
            version != PROOF_VERSION
            or any(
                item.int == 0
                for item in (
                    result.cluster_id,
                    result.authority_epoch,
                    result.worker_identity_id,
                    result.instance_id,
                    result.candidate_id,
                )
            )
            or any(not 0 < item < 2**63 for item in (worker_id, generation, revision))
            or observed >= min(nominal, guard, previous)
            or previous > guard
            or max(nominal, guard) > observed + INSTANCE_LEASE_SECONDS * 1_000_000
        ):
            return None
        return result
    except (binascii.Error, OverflowError, TypeError, ValueError, struct.error):
        return None


def verify_worker_proof_signature(token: object, public_key: object) -> UntrustedLeaseProof | None:
    """Verify a pinned key and expose claims; DB/current-lease checks remain separate."""
    claims = parse_untrusted_proof(token)
    if claims is None or type(public_key) is not bytes or len(public_key) != 32:
        return None
    try:
        assert isinstance(token, str)
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw[PROOF_STRUCT.size :], PROOF_CONTEXT + raw[: PROOF_STRUCT.size]
        )
        return claims
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError, binascii.Error):
        return None
