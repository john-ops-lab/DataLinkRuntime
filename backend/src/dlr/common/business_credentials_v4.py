"""Closed, bounded binding contract shared by the remote Claim children."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CAPABILITY = "business_credentials_v1"
MAX_BINDINGS = 16
MAX_VALUE_BYTES = 4096
MAX_VALUES_BYTES = 16384
MAX_CIPHERTEXT_BYTES = 32768
MAX_CIPHERTEXT_TOTAL_BYTES = 65536
MAX_BINDING_METADATA_BYTES = 8192
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_FIELDS = frozenset({"binding_id", "credential_id", "env_key", "field"})
_CREDENTIAL_FIELDS = {
    "password": frozenset({"username", "password"}),
    "token": frozenset({"token"}),
    "access_key": frozenset({"access_key_id", "access_key_secret"}),
    "secret": frozenset({"value"}),
}
_LEGACY_FIELDS = {"access_key": "access_key_id", "secret_key": "access_key_secret"}
_ALL_FIELDS = frozenset().union(*_CREDENTIAL_FIELDS.values(), _LEGACY_FIELDS)


class BusinessCredentialError(ValueError):
    """Only a stable reason may cross the child/HTTP diagnostic boundary."""

    def __init__(self, reason: str = "credentials_snapshot") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Binding:
    binding_id: int
    credential_id: int
    env_key: str
    field: str


def positive_id(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def bindings(value: Any) -> tuple[Binding, ...]:
    if type(value) is not list:
        raise BusinessCredentialError()
    if len(value) > MAX_BINDINGS:
        raise BusinessCredentialError("credentials_budget")
    result: list[Binding] = []
    ids: set[int] = set()
    keys: set[str] = set()
    for item in value:
        if (
            type(item) is not dict
            or set(item) != _FIELDS
            or not positive_id(item["binding_id"])
            or not positive_id(item["credential_id"])
            or type(item["env_key"]) is not str
            or _ENV_KEY.fullmatch(item["env_key"]) is None
            or type(item["field"]) is not str
            or item["field"] not in _ALL_FIELDS
            or item["binding_id"] in ids
            or item["env_key"] in keys
        ):
            raise BusinessCredentialError()
        ids.add(item["binding_id"])
        keys.add(item["env_key"])
        result.append(Binding(**item))
    return tuple(result)


def credential_field(kind: str, field: str) -> str:
    normalized = _LEGACY_FIELDS.get(field, field) if kind == "access_key" else field
    if normalized not in _CREDENTIAL_FIELDS.get(kind, ()):
        raise BusinessCredentialError()
    return normalized


def values(refs: tuple[Binding, ...], value: Any) -> dict[str, str]:
    if type(value) is not dict or set(value) != {ref.env_key for ref in refs}:
        raise BusinessCredentialError()
    return _bounded_values(value)


def _bounded_values(value: dict[str, str]) -> dict[str, str]:
    if len(value) > MAX_BINDINGS or any(_ENV_KEY.fullmatch(key) is None for key in value):
        raise BusinessCredentialError()
    total = 0
    for item in value.values():
        if type(item) is not str or not item or "\x00" in item:
            raise BusinessCredentialError("credentials_unavailable")
        try:
            size = len(item.encode("utf-8"))
        except UnicodeError:
            raise BusinessCredentialError("credentials_unavailable") from None
        total += size
        if size > MAX_VALUE_BYTES or total > MAX_VALUES_BYTES:
            raise BusinessCredentialError("credentials_budget")
    return dict(value)


def launch_values(environment: dict[str, str]) -> tuple[str, ...]:
    """Read only the values in the same verified per-Attempt launch snapshot."""
    selected = {
        key.removeprefix("DLR_SECRET_"): item
        for key, item in environment.items()
        if key.startswith("DLR_SECRET_")
    }
    if not selected:
        raise BusinessCredentialError()
    checked = _bounded_values(selected)
    return tuple(sorted(set(checked.values()), key=lambda item: (-len(item), item)))
