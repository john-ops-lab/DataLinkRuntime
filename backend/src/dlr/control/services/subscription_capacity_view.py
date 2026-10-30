"""Read-only capacity pressure for presentation, never an admission decision."""

from collections.abc import Mapping
from datetime import datetime, timedelta

from dlr.control.models.subscription import SubscriptionQuota

_KEYS = ("pending_count", "pending_bytes", "history_count", "history_bytes", "dedupe_count")


def capacity_paused(
    *,
    receiving: bool,
    local: SubscriptionQuota | None,
    global_quota: SubscriptionQuota | None,
    local_limits: Mapping[str, int],
    global_limits: Mapping[str, int],
    oldest_pending_at: datetime | None,
    now: datetime,
) -> bool:
    """Project only certain persistent pressure; rate windows are transient.

    A next event always needs positive bytes and one count. Near-full bytes do
    not prove that every smaller event would fail, so only exhausted ceilings
    and existing low-water/oldest-pending holds are projected. Never clear a
    reason, acquire quota locks, reserve capacity, or inspect another adapter.
    """
    if not receiving:
        return False
    age = now - oldest_pending_at if oldest_pending_at is not None else timedelta(0)
    if oldest_pending_at is not None and age >= timedelta(minutes=30):
        return True
    for quota, limits in ((local, local_limits), (global_quota, global_limits)):
        if quota is None:
            continue
        used = {
            key: getattr(quota, key)
            + (
                quota.reserved_control_bytes
                if key.endswith("bytes")
                else quota.reserved_control_count
            )
            for key in _KEYS
        }
        if any(used[key] >= limits[key] for key in _KEYS):
            return True
        if quota.blocked_reason is not None and (
            any(used[key] > limits[key] * 0.8 for key in _KEYS)
            or oldest_pending_at is not None
            and age >= timedelta(minutes=5)
        ):
            return True
    return False
