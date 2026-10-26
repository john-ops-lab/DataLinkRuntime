"""Small source contract. SDK clients stay exclusively inside their owner thread."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session, sessionmaker


class SourceFailure(Exception):
    """Only a bounded machine code crosses the secret-bearing connector boundary."""

    def __init__(
        self,
        code: str,
        disposition: Literal["blocked", "backoff", "capacity"] = "blocked",
        *,
        closed: bool = True,
    ) -> None:
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "source_error"
        self.disposition = disposition
        self.closed = closed
        super().__init__(self.code)


class ApplicationBufferBudget:
    """Process-wide accounting; no allocation or unbounded waiting queue."""

    def __init__(self, limit_bytes: int = 64 * 1024**2) -> None:
        if not 0 < limit_bytes <= 64 * 1024**2:
            raise ValueError("Invalid application buffer limit")
        self.limit_bytes = limit_bytes
        self._used = 0
        self._peak = 0
        self._lock = threading.Lock()

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    @property
    def peak_bytes(self) -> int:
        with self._lock:
            return self._peak

    def reserve(self, size: int) -> bool:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Invalid buffer reservation")
        with self._lock:
            if self._used + size > self.limit_bytes:
                return False
            self._used += size
            self._peak = max(self._peak, self._used)
            return True

    def release(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Invalid buffer release")
        with self._lock:
            if size > self._used:
                raise ValueError("Buffer release exceeds reservations")
            self._used -= size

    def scope(self, limit_bytes: int = 2 * 1024**2) -> ScopedBufferBudget:
        return ScopedBufferBudget(self, limit_bytes)


class ScopedBufferBudget:
    """Per-owner ledger permits deterministic cleanup only after actual native close."""

    def __init__(self, parent: ApplicationBufferBudget, limit_bytes: int) -> None:
        self.parent = parent
        self.limit_bytes = limit_bytes
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    def reserve(self, size: int) -> bool:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Invalid buffer reservation")
        with self._lock:
            if self._used + size > self.limit_bytes or not self.parent.reserve(size):
                return False
            self._used += size
            return True

    def release(self, size: int) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("Invalid buffer release")
        with self._lock:
            if size > self._used:
                raise ValueError("Buffer release exceeds owner reservations")
            self.parent.release(size)
            self._used -= size

    def release_all(self) -> None:
        with self._lock:
            self.parent.release(self._used)
            self._used = 0


@dataclass(frozen=True)
class SourceRunContext:
    subscription_id: int
    owner_id: str
    generation: int
    activation_id: int
    config_version: int
    source_epoch: str
    source_config: dict[str, Any] = field(repr=False)
    credential_fields: dict[str, str] = field(repr=False)
    stop_event: threading.Event = field(repr=False)
    session_factory: sessionmaker[Session] = field(repr=False)
    buffer: ScopedBufferBudget = field(repr=False)
    connected: Callable[[], None] = field(repr=False)
    identity_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    public_id: str | None = None


class SourceRunner(Protocol):
    def run(self, context: SourceRunContext) -> None:
        """Return/raise only after closing; use closed=False if close cannot be confirmed."""
        ...

    def test_connection(
        self, source_config: dict[str, Any], credential_fields: dict[str, str]
    ) -> dict[str, Any]:
        """Bounded diagnostic; no group join, source ACK, checkpoint or business execution."""
        ...
