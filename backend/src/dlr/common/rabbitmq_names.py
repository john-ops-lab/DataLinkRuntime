"""Stable dispatch queue names shared by Control and standalone Workers."""

WORK_QUEUE_PREFIX = "dlr.worker."
WORK_QUEUE_SUFFIX = ".q"


def worker_queue_name(worker_id: int) -> str:
    """Return the existing fixed-Worker dispatch queue, not a Pool queue."""
    if type(worker_id) is not int or not 0 < worker_id < 2**63:
        raise ValueError("invalid Worker ID")
    return f"{WORK_QUEUE_PREFIX}{worker_id}{WORK_QUEUE_SUFFIX}"
