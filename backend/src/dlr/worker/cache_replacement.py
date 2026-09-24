"""Thread-local authority for a real Attempt's cache replacement prepare."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReplacementAuthority:
    client: Any
    worker_id: int
    payload: Mapping[str, Any]
    attempt_journal_root: Path
    cleanup_journal_root: Path


_local = threading.local()


@contextmanager
def activate(authority: ReplacementAuthority) -> Iterator[None]:
    previous = getattr(_local, "authority", None)
    _local.authority = authority
    try:
        yield
    finally:
        _local.authority = previous


def current() -> ReplacementAuthority | None:
    return getattr(_local, "authority", None)
