"""Bounded HTTPS client for an externally supplied PostgreSQL capacity authority.

This module does not establish the current topology, start a poller, or grant
admission. Callers must supply a separately verified current authority and
fetch outside database transactions. A fixture response is not evidence of a
real database capacity domain.
"""

import http.client
import json
import math
import os
import queue
import secrets
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

_MAX_BODY = 16384
_MAX_IDENTIFIER = 256
_MAX_INTEGER = 2**63 - 1
_MAX_AGE = timedelta(seconds=15)
_MAX_FUTURE = timedelta(seconds=5)
_PROCESS_TAG = secrets.token_bytes(32)


def _read_clocks() -> tuple[int, datetime, int] | None:
    """Bracket wall time with a monotonic clock that includes system suspend."""
    try:
        if sys.platform.startswith("linux"):
            clock = time.CLOCK_BOOTTIME
        elif sys.platform == "darwin":
            clock = time.CLOCK_MONOTONIC
        else:
            return None
        before = time.clock_gettime_ns(clock)
        wall = datetime.now(UTC)
        after = time.clock_gettime_ns(clock)
        return (before, wall, after) if 0 <= before <= after else None
    except (AttributeError, OSError, OverflowError, ValueError):
        return None


class CapacityUnavailable(Exception):
    """The provider could not supply a valid observation; contains no remote data."""


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_IDENTIFIER
        and value.isascii()
        and all(33 <= ord(char) <= 126 for char in value)
    )


def _epoch(value: object) -> bool:
    return _identifier(value) or type(value) is int and 0 <= value <= _MAX_INTEGER


@dataclass(frozen=True)
class VerifiedCapacityAuthority:
    """Caller-supplied current identity; constructing it is not verification."""

    cluster_id: str
    storage_domain_id: str
    topology_epoch: str | int

    def __post_init__(self) -> None:
        if not _identifier(self.cluster_id) or not _identifier(self.storage_domain_id):
            raise ValueError("Invalid capacity authority")
        if not _epoch(self.topology_epoch):
            raise ValueError("Invalid capacity authority")


@dataclass
class _ClockState:
    wall: datetime
    before: int
    after: int
    invalid: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True)
class CapacityObservation:
    authority: VerifiedCapacityAuthority
    observed_at: datetime
    fetched_at: datetime
    writable_free_bytes: int
    _pid: int = field(repr=False)
    _process_tag: bytes = field(repr=False)
    _tick_before: int = field(repr=False)
    _clock_state: _ClockState = field(repr=False, compare=False)

    def usable(self, authority: VerifiedCapacityAuthority) -> bool:
        if self._pid != os.getpid() or self._process_tag != _PROCESS_TAG:
            return False
        with self._clock_state.lock:
            state = self._clock_state
            clocks = _read_clocks()
            if state.invalid or clocks is None:
                state.invalid = True
                return False
            before, now, after = clocks
            if before < state.after or after - self._tick_before > 15_000_000_000:
                state.invalid = True
                return False
            # Wall readings have microsecond precision. Clock reads bracket each
            # wall reading; each pair provides a lower bound on elapsed time.
            wall_step = now - state.wall
            wall_ns = (wall_step.days * 86400 + wall_step.seconds) * 1_000_000_000
            wall_ns += wall_step.microseconds * 1000
            if wall_ns < before - state.after - 2000:
                state.invalid = True
                return False
            state.wall, state.before, state.after = now, before, after
            wall_elapsed = now - self.fetched_at
            return (
                self.authority == authority
                and timedelta(0) <= wall_elapsed <= _MAX_AGE
                and -_MAX_FUTURE <= now - self.observed_at <= _MAX_AGE
            )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _invalid_constant(_: str) -> Any:
    raise ValueError("Invalid constant")


def parse_observation(body: bytes, authority: VerifiedCapacityAuthority) -> CapacityObservation:
    try:
        clocks = _read_clocks()
        if len(body) > _MAX_BODY or clocks is None:
            raise ValueError("Invalid observation")
        tick_before, fetched_at, tick_after = clocks
        value = json.loads(body, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        fields = {
            "schema_version",
            "cluster_id",
            "storage_domain_id",
            "topology_epoch",
            "observed_at",
            "writable_free_bytes",
        }
        if not isinstance(value, dict) or value.keys() != fields:
            raise ValueError("Invalid fields")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("Invalid schema")
        if (
            value["cluster_id"] != authority.cluster_id
            or value["storage_domain_id"] != authority.storage_domain_id
            or type(value["topology_epoch"]) is not type(authority.topology_epoch)
            or value["topology_epoch"] != authority.topology_epoch
        ):
            raise ValueError("Invalid identity")
        timestamp = value["observed_at"]
        if not isinstance(timestamp, str) or len(timestamp) > 64:
            raise ValueError("Invalid timestamp")
        observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Missing timezone")
        free_bytes = value["writable_free_bytes"]
        if type(free_bytes) is not int or not 0 <= free_bytes <= _MAX_INTEGER:
            raise ValueError("Invalid capacity")
        observation = CapacityObservation(
            authority,
            observed_at,
            fetched_at,
            free_bytes,
            os.getpid(),
            _PROCESS_TAG,
            tick_before,
            _ClockState(fetched_at, tick_before, tick_after),
        )
        if not observation.usable(authority):
            raise ValueError("Stale or future observation")
        return observation
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise CapacityUnavailable("Capacity observation unavailable") from None


class PostgresCapacityProvider:
    """A fixed authenticated endpoint; only one in-flight fetch per instance."""

    def __init__(
        self,
        endpoint: str,
        bearer_token: str,
        *,
        ca_file: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port or 443
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not 1 <= port <= 65535
                or not parsed.path.startswith("/")
                or any(ord(c) < 33 or ord(c) > 126 for c in endpoint)
            ):
                raise ValueError
            if (
                not bearer_token
                or len(bearer_token) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in bearer_token)
            ):
                raise ValueError
            if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 5:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid capacity provider configuration") from None
        self._host = parsed.hostname
        self._port = port
        self._path = parsed.path
        self._token = bearer_token
        self._timeout = timeout_seconds
        self._tls = ssl.create_default_context(cafile=ca_file)
        self._lock = threading.Lock()

    def fetch(self, authority: VerifiedCapacityAuthority) -> CapacityObservation:
        """Return within the deadline; a stuck resolver cannot spawn another fetch.

        The one daemon worker retains the instance lock until it actually exits.
        The caller must reuse this provider instance, not construct one per request.
        """
        if not self._lock.acquire(blocking=False):
            raise CapacityUnavailable("Capacity observation unavailable")
        result: queue.Queue[CapacityObservation | None] = queue.Queue(maxsize=1)
        deadline = time.monotonic() + self._timeout

        def run() -> None:
            try:
                result.put_nowait(self._fetch(authority, deadline))
            except Exception:
                result.put_nowait(None)
            finally:
                self._lock.release()

        try:
            threading.Thread(target=run, daemon=True, name="pg-capacity-provider").start()
        except Exception:
            self._lock.release()
            raise CapacityUnavailable("Capacity observation unavailable") from None
        try:
            observation = result.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise CapacityUnavailable("Capacity observation unavailable") from None
        if observation is None or time.monotonic() > deadline:
            raise CapacityUnavailable("Capacity observation unavailable")
        return observation

    def _fetch(self, authority: VerifiedCapacityAuthority, deadline: float) -> CapacityObservation:
        connection = None
        try:
            connection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout, context=self._tls
            )
            connection.request(
                "GET",
                self._path,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if (
                response.status != 200
                or response.getheader("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                raise CapacityUnavailable("Capacity observation unavailable")
            body = bytearray()
            while len(body) <= _MAX_BODY:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CapacityUnavailable("Capacity observation unavailable")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(4096, _MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if time.monotonic() > deadline:
                raise CapacityUnavailable("Capacity observation unavailable")
            return parse_observation(bytes(body), authority)
        finally:
            if connection is not None:
                connection.close()
