"""Local shared/runtime credential separation checks, not a cluster fence.

The local stored-collision check runs on shared Worker routes only when fresh
v4 bootstrap is enabled. The candidate check and global coordination primitive
remain separate. Every Control must share the same configured token, enforce
issuance checks, use the same authoritative database, and fence older binaries
before this could form a cluster guarantee.
"""

from __future__ import annotations

import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import WorkerCredential
from dlr.control.services import worker_credentials
from dlr.control.services.adapter import domain_error


class _RedactedDigest(bytes):
    """Keep ORM debug/error formatting from exposing a bound digest."""

    def __repr__(self) -> str:
        return "<redacted>"


def check_candidate(shared_token: str | None, candidate: str) -> None:
    """Reject issuing an identical runtime secret without returning either value."""
    if (
        shared_token is not None
        and shared_token.isascii()
        and hmac.compare_digest(shared_token, candidate)
    ):
        raise worker_credentials.WorkerCredentialError("worker_credential_unavailable")


def assert_no_persisted_collision(session: Session, shared_token: str | None) -> None:
    """Reject a shared token matching any stored Worker credential.

    Revoked and expired credentials remain relevant because the holder still
    knows the string. Non-v4-hex legacy tokens cannot equal a runtime secret.
    The query has no locks and is not an issuance/configuration race fence.
    """
    if shared_token is None:
        return
    try:
        digest = worker_credentials._digest(shared_token)
    except worker_credentials.WorkerCredentialError:
        return
    collision = session.scalar(
        select(WorkerCredential.id)
        .where(
            WorkerCredential.secret_digest == _RedactedDigest(digest),
        )
        .limit(1)
    )
    if collision is not None:
        raise domain_error(503, "worker_credentials_conflict", "Worker credentials unavailable")
