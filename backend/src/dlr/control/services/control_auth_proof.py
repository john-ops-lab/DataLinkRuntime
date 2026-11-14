"""Private cluster-auth comparison primitives; not a Control admission gate.

Authority creation is an explicit deployment operation. These primitives do
not create authority, verify an image or a remote host, register an instance,
or enable readiness. Master keys must have adequate entropy: possession of a
proof permits offline guesses of a weak master key, just as existing encrypted
credential material does. Proof values and configuration never belong in logs,
API responses or metrics.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_CONTEXT = b"dlr/control-cluster-auth-proof/v1"
_MAX_SECRET_BYTES = 4096


class ControlAuthUnavailable(Exception):
    """Invalid or unmatched cluster authentication material, without details."""


@dataclass(frozen=True)
class ControlAuthProof:
    """Private keyed comparators stored only in an access-controlled authority."""

    master_check: bytes = field(repr=False)
    admin_check: bytes = field(repr=False)
    worker_check: bytes = field(repr=False)


def _secret(value: str) -> bytes:
    if not isinstance(value, str):
        raise ControlAuthUnavailable("cluster authentication unavailable")
    try:
        result = value.encode("utf-8")
    except UnicodeError:
        raise ControlAuthUnavailable("cluster authentication unavailable") from None
    if not 1 <= len(result) <= _MAX_SECRET_BYTES:
        raise ControlAuthUnavailable("cluster authentication unavailable")
    return result


def _derive(master: str, cluster_id: UUID) -> bytes:
    if not isinstance(cluster_id, UUID):
        raise ControlAuthUnavailable("cluster authentication unavailable")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=cluster_id.bytes, info=_CONTEXT).derive(
        _secret(master)
    )


def _mac(key: bytes, label: bytes, value: bytes) -> bytes:
    return hmac.new(key, _CONTEXT + b"\x00" + label + b"\x00" + value, hashlib.sha256).digest()


def create_auth_proof(
    *, cluster_id: UUID, master_key: str, admin_token: str, worker_token: str
) -> ControlAuthProof:
    """Create versioned, domain-separated keyed checks; never return secrets."""
    key = _derive(master_key, cluster_id)
    return ControlAuthProof(
        master_check=_mac(key, b"master", cluster_id.bytes),
        admin_check=_mac(key, b"admin", _secret(admin_token)),
        worker_check=_mac(key, b"worker", _secret(worker_token)),
    )


def verify_auth_proof(
    expected: ControlAuthProof,
    *,
    cluster_id: UUID,
    master_key: str,
    admin_token: str,
    worker_token: str,
) -> bool:
    """Compare all values without disclosing which credential mismatched."""
    if type(expected) is not ControlAuthProof or any(
        type(value) is not bytes or len(value) != 32
        for value in (expected.master_check, expected.admin_check, expected.worker_check)
    ):
        return False
    try:
        actual = create_auth_proof(
            cluster_id=cluster_id,
            master_key=master_key,
            admin_token=admin_token,
            worker_token=worker_token,
        )
    except ControlAuthUnavailable:
        return False
    # Compare each field before combining, so an early mismatch does not reveal
    # which member failed. The caller supplies only a generic failure result.
    checks = (
        hmac.compare_digest(actual.master_check, expected.master_check),
        hmac.compare_digest(actual.admin_check, expected.admin_check),
        hmac.compare_digest(actual.worker_check, expected.worker_check),
    )
    return all(checks)
