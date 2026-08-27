"""C0 Worker protocol primitives shared by claim and report endpoints.

Only one-way token digests cross the database boundary.  Raw token values are
returned to a v2 Worker from the claim response and are never logged or
serialized into an Execution response.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from dlr.control.services.adapter import domain_error

TOKEN_BYTES = 32
CLAIM_TOKEN_HEADER = "X-DLR-Claim-Token"
CLEANUP_TOKEN_HEADER = "X-DLR-Cleanup-Token"


def generate_token() -> str:
    """Generate a URL-safe representation of exactly 256 random bits."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the database-safe one-way SHA-256 digest for one token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str | None, expected_hash: str | None) -> bool:
    """Compare a supplied token and stored digest without early equality."""
    if token is None or expected_hash is None:
        return False
    candidate = hash_token(token)
    return hmac.compare_digest(candidate, expected_hash)


def require_claim_token(token: str | None, expected_hash: str | None) -> None:
    """Reject missing, swapped or invalid Claim credentials consistently."""
    if not token_matches(token, expected_hash):
        raise domain_error(
            422,
            "execution_claim_token_invalid",
            "A valid Claim Token is required",
        )


def require_cleanup_token(token: str | None, expected_hash: str | None) -> None:
    """Reject missing, swapped or invalid Cleanup credentials consistently."""
    if not token_matches(token, expected_hash):
        raise domain_error(
            422,
            "execution_cleanup_token_invalid",
            "A valid Cleanup Token is required",
        )
