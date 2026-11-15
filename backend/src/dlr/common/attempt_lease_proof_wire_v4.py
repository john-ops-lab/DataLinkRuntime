"""Wire constants for the experimental Attempt proof, not authorization."""

import base64
import binascii
import struct
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS

ATTEMPT_PROOF_VERSION = 2
ATTEMPT_PROOF_STRUCT = struct.Struct(">B16s16sQ16s16sQQQQ16sqqqq")
ATTEMPT_PROOF_CONTEXT = b"dlr/attempt-lease-proof/v2\x00"
ATTEMPT_PROOF_SIGNATURE_BYTES = 64
ATTEMPT_PROOF_BYTES = ATTEMPT_PROOF_STRUCT.size + ATTEMPT_PROOF_SIGNATURE_BYTES
ATTEMPT_PROOF_TOKEN_TEXT_LENGTH = ((ATTEMPT_PROOF_BYTES + 2) // 3) * 4
ATTEMPT_PROOF_HEADER = "X-DLR-Worker-Attempt-Proof"
# Only this HTTP/1-style header line is included; it is not a full network or
# memory budget and does not prove HTTP/2 framing or parser allocation bounds.
ATTEMPT_PROOF_HEADER_WIRE_BYTES = (
    len(ATTEMPT_PROOF_HEADER.encode("ascii")) + 2 + ATTEMPT_PROOF_TOKEN_TEXT_LENGTH + 2
)


@dataclass(frozen=True)
class UntrustedAttemptProof:
    cluster_id: UUID
    authority_epoch: UUID
    worker_id: int
    worker_identity_id: UUID
    instance_id: UUID
    generation: int
    attempt_id: int
    fencing_token: int
    revision: int
    candidate_id: UUID
    nominal_us: int
    guard_us: int
    proof_before_us: int
    observed_us: int


def parse_untrusted_attempt_proof(token: object) -> UntrustedAttemptProof | None:
    """Parse canonical public fields only; no signature verification."""
    try:
        if type(token) is not str or len(token) != ATTEMPT_PROOF_TOKEN_TEXT_LENGTH:
            return None
        encoded = token.encode("ascii")
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if len(raw) != ATTEMPT_PROOF_BYTES or base64.urlsafe_b64encode(raw) != encoded:
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
            revision,
            candidate,
            nominal,
            guard,
            previous,
            observed,
        ) = ATTEMPT_PROOF_STRUCT.unpack(raw[: ATTEMPT_PROOF_STRUCT.size])
        result = UntrustedAttemptProof(
            UUID(bytes=cluster),
            UUID(bytes=epoch),
            worker,
            UUID(bytes=identity),
            UUID(bytes=instance),
            generation,
            attempt,
            fence,
            revision,
            UUID(bytes=candidate),
            nominal,
            guard,
            previous,
            observed,
        )
        if (
            version != ATTEMPT_PROOF_VERSION
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
            or any(not 0 < item < 2**63 for item in (worker, generation, attempt, fence, revision))
            or observed >= min(nominal, guard, previous)
            or previous > guard
            or max(nominal, guard) > observed + INSTANCE_LEASE_SECONDS * 1_000_000
        ):
            return None
        return result
    except (binascii.Error, OverflowError, TypeError, ValueError, struct.error):
        return None


def verify_attempt_proof_signature(
    token: object, public_key: object
) -> UntrustedAttemptProof | None:
    """Verify the pinned key; DB/current-Attempt authorization remains separate."""
    claims = parse_untrusted_attempt_proof(token)
    if claims is None or type(public_key) is not bytes or len(public_key) != 32:
        return None
    try:
        assert isinstance(token, str)
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw[ATTEMPT_PROOF_STRUCT.size :],
            ATTEMPT_PROOF_CONTEXT + raw[: ATTEMPT_PROOF_STRUCT.size],
        )
        return claims
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError, binascii.Error):
        return None
