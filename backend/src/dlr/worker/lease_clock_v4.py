"""Conservative local timing primitives for an experimental v4 instance."""

import math
import sys
import time

from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS, V4_LEASE_CLOCK_MARGIN_NS


def local_lease_deadline(request_start: float) -> float:
    duration = INSTANCE_LEASE_SECONDS - V4_LEASE_CLOCK_MARGIN_NS / 1_000_000_000
    deadline = request_start + duration
    # Conversion and addition can both round upward. Bias the deadline earlier.
    return math.nextafter(math.nextafter(deadline, -math.inf), -math.inf)


def lease_clock() -> float | None:
    """Elapsed time including host suspend, or unavailable on unknown platforms."""
    try:
        if sys.platform.startswith("linux"):
            return time.clock_gettime(time.CLOCK_BOOTTIME)
        if sys.platform == "darwin":
            return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError, ValueError):
        pass
    return None
