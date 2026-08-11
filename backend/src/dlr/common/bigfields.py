"""Shared big-field truncation rules (M2 spec §5).

Used by both the Control Node (re-validating worker reports) and the Worker
(capping subprocess streams), so both sides apply identical limits.
"""


def truncate_utf8(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """Cap UTF-8 data at ``max_bytes``, keeping head and tail.

    When truncation happens the head and tail are preserved with an explicit
    marker in between, so tracebacks at the end of a stream stay visible.
    """
    if len(data) <= max_bytes:
        return data, False
    # Reserve a fixed budget for the marker line.
    keep = max_bytes - 64
    head_len = keep // 2
    tail_len = keep - head_len
    omitted = len(data) - head_len - tail_len
    marker = f"\n...[truncated {omitted} bytes]...\n".encode()
    return data[:head_len] + marker + data[len(data) - tail_len :], True
