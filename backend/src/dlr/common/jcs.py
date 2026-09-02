"""Small RFC 8785/JCS helpers used by reliable ingress.

The dependency gate pins ``rfc8785`` and this module deliberately exposes no
``sort_keys`` fallback: payload identity must be the same across clients for
numbers, Unicode and object-property ordering as well as insignificant JSON
whitespace.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import rfc8785

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,128}\Z")


class CanonicalizationInputError(ValueError):
    """A JSON value outside RFC 8785's canonical number domain."""


def canonicalize(value: Any) -> bytes:
    """Return RFC 8785 canonical UTF-8 bytes for a JSON-compatible value."""

    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError):
        # Do not carry a user-supplied number into an API error or a log
        # traceback.  Callers map this stable domain failure to 422.
        raise CanonicalizationInputError(
            "value is outside the RFC 8785 canonical JSON domain"
        ) from None


def payload_hash(trigger: str, body: Any) -> bytes:
    """Hash the canonical request identity, including explicit JSON ``null``."""

    return hashlib.sha256(canonicalize({"trigger": trigger, "body": body})).digest()


def key_hash(key: str) -> bytes:
    """Hash one validated raw Idempotency-Key without retaining its value."""

    validate_idempotency_key(key)
    return hashlib.sha256(key.encode("ascii")).digest()


def validate_idempotency_key(value: object) -> str:
    """Validate the closed 1..128 visible-ASCII Idempotency-Key contract."""

    if not isinstance(value, str) or _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("Idempotency-Key must be 1-128 visible ASCII characters")
    return value
