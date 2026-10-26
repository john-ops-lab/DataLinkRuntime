"""Deployment limits and their durable receiving fence.

Only new T1 custody is gated here. Readback, terminal settlement, replay of an
already accepted responsibility and quota cleanup must not call these guards.
Callers own the transaction and must hold the global quota lock while checking
or adopting the fingerprint. No source I/O is performed under that lock.
"""

import hashlib
import json
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models.ingress_capacity import (
    IngressCapacityAudit,
    IngressCapacityConfiguration,
)
from dlr.control.models.subscription import SubscriptionLease, SubscriptionQuota
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now

DEFAULT_SUBSCRIPTION_LIMITS = {
    "pending_count": 1000,
    "pending_bytes": 32 * 1024**2,
    "history_count": 10000,
    "history_bytes": 256 * 1024**2,
    "dedupe_count": 500000,
    "rate": 10,
}
DEFAULT_GLOBAL_LIMITS = {
    "pending_count": 10000,
    "pending_bytes": 256 * 1024**2,
    "history_count": 100000,
    "history_bytes": 1024**3,
    "dedupe_count": 500000,
    "rate": 50,
}
DISK_STOP_BYTES = 5 * 1024**3
DISK_RESUME_BYTES = 6 * 1024**3
_MAX_PROFILE_BYTES = 16384
_MAX_STORED_INTEGER = 2**63 - 1


class CapacityProfileError(ValueError):
    """Invalid non-sensitive deployment capacity configuration."""


@dataclass(frozen=True)
class CapacityProfile:
    global_limits: Mapping[str, int]
    subscription_max: Mapping[str, int]
    fingerprint: str

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {"global": dict(self.global_limits), "subscription_max": dict(self.subscription_max)}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CapacityProfileError("Duplicate capacity field")
        value[key] = item
    return value


def _non_json_constant(_: str) -> Any:
    raise CapacityProfileError("Capacity values must be JSON integers")


def _limits(value: Any, defaults: dict[str, int]) -> dict[str, int]:
    if not isinstance(value, dict) or value.keys() - defaults.keys():
        raise CapacityProfileError("Unknown capacity field or invalid capacity object")
    result = dict(defaults)
    for key, amount in value.items():
        if type(amount) is not int or not 1 <= amount <= _MAX_STORED_INTEGER:
            raise CapacityProfileError("Capacity must be a positive storage-safe integer")
        result[key] = amount
    if result["pending_count"] > result["history_count"]:
        raise CapacityProfileError("Pending count cannot exceed retained history capacity")
    if result["history_count"] > result["dedupe_count"]:
        raise CapacityProfileError("History count cannot exceed deduplication capacity")
    if result["pending_bytes"] > result["history_bytes"]:
        raise CapacityProfileError("Pending bytes cannot exceed retained history capacity")
    # Every durable responsibility costs at least one byte; disallow profiles
    # whose declared count cannot fit even this minimum billable size.
    if result["pending_count"] > result["pending_bytes"]:
        raise CapacityProfileError("Pending bytes cannot hold the configured count")
    if result["history_count"] > result["history_bytes"]:
        raise CapacityProfileError("History bytes cannot hold the configured count")
    return result


@lru_cache(maxsize=16)
def parse_profile(raw: str | None) -> CapacityProfile:
    if raw is None or raw == "":
        value: Any = {}
    else:
        try:
            if len(raw.encode("utf-8")) > _MAX_PROFILE_BYTES:
                raise CapacityProfileError("Capacity configuration is too large")
            value = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_non_json_constant
            )
        except (ValueError, RecursionError) as error:
            raise CapacityProfileError("Invalid capacity JSON") from error
    if not isinstance(value, dict) or value.keys() - {"global", "subscription_max"}:
        raise CapacityProfileError("Unknown capacity configuration field")
    global_values = _limits(value.get("global", {}), DEFAULT_GLOBAL_LIMITS)
    subscription_max = _limits(value.get("subscription_max", {}), DEFAULT_SUBSCRIPTION_LIMITS)
    if any(subscription_max[key] > global_values[key] for key in global_values):
        raise CapacityProfileError("Subscription maximum cannot exceed global capacity")
    canonical = json.dumps(
        {"global": global_values, "subscription_max": subscription_max},
        sort_keys=True,
        separators=(",", ":"),
    )
    return CapacityProfile(
        MappingProxyType(global_values),
        MappingProxyType(subscription_max),
        hashlib.sha256(canonical.encode("ascii")).hexdigest(),
    )


def configured_profile() -> CapacityProfile:
    return parse_profile(settings.ingress_capacity_profile)


def subscription_limits(config: dict[str, Any]) -> dict[str, int]:
    """Existing entry defaults remain unchanged; explicit limits may use the profile max."""
    try:
        maximum = configured_profile().subscription_max
    except CapacityProfileError as error:
        raise domain_error(
            503, "ingress_capacity_unavailable", "Capacity profile is invalid"
        ) from error
    values = {key: min(amount, maximum[key]) for key, amount in DEFAULT_SUBSCRIPTION_LIMITS.items()}
    supplied = config.get("limits", {})
    if not isinstance(supplied, dict) or supplied.keys() - maximum.keys():
        raise domain_error(422, "subscription_config_invalid", "Invalid capacity limit")
    for key, amount in supplied.items():
        if type(amount) is not int or not 0 < amount <= maximum[key]:
            raise domain_error(
                422, "subscription_config_invalid", "Capacity exceeds deployment maximum"
            )
        values[key] = amount
    return values


def effective_profile(session: Session) -> CapacityProfile:
    """The adopted deployment governs retained work, independent of local drift.

    Before the first adoption, existing responsibilities were created using
    the historical defaults. Invalid new local JSON cannot revoke that budget.
    """
    row = session.get(IngressCapacityConfiguration, 1, populate_existing=True)
    if row is not None:
        return parse_profile(json.dumps(row.profile))
    try:
        return configured_profile()
    except CapacityProfileError:
        return parse_profile(None)


def runtime_subscription_limits(session: Session, config: dict[str, Any]) -> dict[str, int]:
    """Read retained configuration after a deployment reduction without rejecting it."""
    return runtime_limits_for_profile(effective_profile(session), config)


def runtime_limits_for_profile(profile: CapacityProfile, config: dict[str, Any]) -> dict[str, int]:
    """Pure entry calculation from one already-read durable profile."""
    maximum = profile.subscription_max
    result = {key: min(value, maximum[key]) for key, value in DEFAULT_SUBSCRIPTION_LIMITS.items()}
    for key, value in config.get("limits", {}).items():
        if key in maximum and type(value) is int and value > 0:
            result[key] = min(value, maximum[key])
    return result


def global_limits(
    session: Session | None = None, *, profile: CapacityProfile | None = None
) -> dict[str, int]:
    if profile is not None:
        return dict(profile.global_limits)
    if session is not None:
        return dict(effective_profile(session).global_limits)
    try:
        return dict(configured_profile().global_limits)
    except CapacityProfileError as error:
        raise domain_error(
            503, "ingress_capacity_unavailable", "Capacity profile is invalid"
        ) from error


def _lock_global_quota(session: Session) -> None:
    session.execute(insert(SubscriptionQuota).values(scope="global").on_conflict_do_nothing())
    row = session.get(SubscriptionQuota, "global", with_for_update=True, populate_existing=True)
    assert row is not None


def _storage_budget_available(profile: CapacityProfile) -> bool:
    """Reserve the 5 GiB stop margin before adopting a retained-byte ceiling.

    Pending payload is already included in history accounting, so it must not
    be added a second time. The continuous disk guard also covers WAL and
    unrelated growth; this is not a promise that the ceiling reserves space.
    """
    try:
        free_bytes = shutil.disk_usage(Path(settings.ingress_storage_root)).free
    except OSError:
        return False
    return free_bytes - DISK_STOP_BYTES >= profile.global_limits["history_bytes"]


def activate_profile(session: Session) -> bool:
    """Adopt at listener startup, before acquiring a source lease.

    A deployment change can only replace the durable fingerprint after every
    old listener lease has expired. Existing owners whose local fingerprint
    differs subsequently fail closed at new T1. Adoption and custody serialize
    on the same global quota row; this never resets its retained counters.
    """
    try:
        profile = configured_profile()
    except CapacityProfileError:
        return False
    storage_available = _storage_budget_available(profile)
    _lock_global_quota(session)
    row = session.get(IngressCapacityConfiguration, 1, with_for_update=True, populate_existing=True)
    if row is not None and row.fingerprint == profile.fingerprint:
        return True
    if not storage_available:
        return False
    if row is not None and session.scalar(
        select(exists().where(SubscriptionLease.lease_until > database_now(session)))
    ):
        return False
    previous = row.fingerprint if row is not None else None
    if row is None:
        row = IngressCapacityConfiguration(
            id=1, fingerprint=profile.fingerprint, profile=profile.as_dict()
        )
        session.add(row)
    else:
        row.fingerprint = profile.fingerprint
        row.profile = profile.as_dict()
        row.adopted_at = database_now(session)
    session.add(
        IngressCapacityAudit(
            previous_fingerprint=previous,
            fingerprint=profile.fingerprint,
            profile=profile.as_dict(),
        )
    )
    session.flush()
    return True


def require_active_profile(session: Session) -> CapacityProfile:
    """Called after locking quotas in T1, and only for a genuinely new event."""
    try:
        profile = configured_profile()
    except CapacityProfileError as error:
        raise domain_error(
            503, "ingress_capacity_unavailable", "Capacity profile is invalid"
        ) from error
    row = session.get(IngressCapacityConfiguration, 1, populate_existing=True)
    if row is None:
        activate_profile(session)
        row = session.get(IngressCapacityConfiguration, 1, populate_existing=True)
    if row is None or row.fingerprint != profile.fingerprint:
        raise domain_error(
            503, "ingress_capacity_unavailable", "Capacity profile does not match deployment"
        )
    # T1 holds the global quota lock until commit. Every legitimate adoption
    # takes that same lock, so this freshly-read row governs both ceilings for
    # this custody transaction. No profile state survives the transaction.
    return parse_profile(json.dumps(row.profile))


class DiskCapacityGuard:
    """Stop at 5 GiB, resume at 6 GiB; a restart cannot bypass the low water mark."""

    def __init__(self) -> None:
        self._blocked = True
        self._lock = threading.Lock()

    def check(self, free_bytes: int) -> None:
        with self._lock:
            if free_bytes <= DISK_STOP_BYTES:
                self._blocked = True
            elif free_bytes >= DISK_RESUME_BYTES:
                self._blocked = False
            if self._blocked:
                raise domain_error(503, "ingress_disk_low", "Persistent storage is below low water")


_disk_guard = DiskCapacityGuard()


def require_disk_capacity() -> None:
    try:
        free_bytes = shutil.disk_usage(Path(settings.ingress_storage_root)).free
    except OSError as error:
        raise domain_error(
            503, "ingress_disk_unavailable", "Persistent storage is unavailable"
        ) from error
    _disk_guard.check(free_bytes)
