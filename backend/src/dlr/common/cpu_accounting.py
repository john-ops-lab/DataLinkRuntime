"""Conservative CPU charges for the existing cgroup v2 quota contract."""

import math
from decimal import Decimal

CPU_PERIOD_US = 100_000
CPU_MIN_QUOTA_US = 1_000
CPU_MIN_MILLIS = (CPU_MIN_QUOTA_US * 1000 + CPU_PERIOD_US - 1) // CPU_PERIOD_US
# Version 1 predates minimum-quota and context-independent accounting.
CPU_ACCOUNTING_VERSION = 2


def cpu_units(value: float, *, round_up: bool) -> int:
    """Convert CPU cores to units of one cgroup period, with explicit rounding."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid CPU value")
    if value < 0:
        raise ValueError("invalid CPU value")
    # Decimal multiplication would depend on the caller's thread-local
    # precision and rounding mode. Integer ratios keep both directions exact.
    numerator, denominator = Decimal(str(value)).as_integer_ratio()
    units, remainder = divmod(numerator * CPU_PERIOD_US, denominator)
    return units + int(round_up and remainder != 0)


def cpu_quota_us(value: float) -> int:
    """Preserve the Worker's existing quota rounding and minimum."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid CPU value")
    if value <= 0:
        raise ValueError("invalid CPU value")
    scaled = value * CPU_PERIOD_US
    if not math.isfinite(scaled):
        raise ValueError("invalid CPU value")
    return max(CPU_MIN_QUOTA_US, int(round(scaled)))


def attempt_cpu_units(value: float) -> int:
    """Charge at least both the requested CPU and the actual cgroup quota."""
    requested = cpu_units(value, round_up=True)
    try:
        quota = cpu_quota_us(value)
    except ValueError:
        # Oversized finite requests are still rejected by capacity comparison,
        # without requiring the scheduler to construct a kernel quota.
        if requested <= 0:
            raise
        quota = requested
    return max(requested, quota)


def attempt_cpu_millis(value: float) -> int:
    return (attempt_cpu_units(value) + CPU_PERIOD_US // 1000 - 1) // (CPU_PERIOD_US // 1000)
