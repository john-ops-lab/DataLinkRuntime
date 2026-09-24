"""Validated Worker cache policy and the single version-cache factory."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dlr.worker.cache import CacheError, CacheReservation, VerifiedVersionCache


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _integer(
    environ: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int | None
) -> int:
    raw = environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"-{maximum}" if maximum is not None else " or greater"
        raise ValueError(f"{name} must be {minimum}{suffix}")
    return value


@dataclass(frozen=True)
class CachePolicy:
    gc_enabled: bool = False
    pressure_gc_enabled: bool = False
    scan_interval_seconds: int = 300
    idle_ttl_seconds: int = 2_592_000
    min_idle_seconds: int = 86_400
    max_bytes: int = 4_294_967_296
    high_watermark_percent: int = 85
    low_watermark_percent: int = 70
    disk_reserve_bytes: int = 134_217_728
    max_delete_bytes_per_round: int = 268_435_456
    max_delete_entries_per_round: int = 20
    max_scan_entries_per_round: int = 200
    max_scan_nodes_per_round: int = 100_000
    max_scan_hash_bytes_per_round: int = 268_435_456
    max_scan_depth: int = 64
    max_round_seconds: int = 10
    staging_ttl_seconds: int = 86_400
    offline_protection: bool = True
    offline_mode: bool = False
    shared_cache_mode: str = "report_only"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> CachePolicy:
        source = os.environ if environ is None else environ
        values: dict[str, Any] = {
            "gc_enabled": _boolean(source, "DLR_CACHE_GC_ENABLED", False),
            "pressure_gc_enabled": _boolean(source, "DLR_CACHE_PRESSURE_GC_ENABLED", False),
            "scan_interval_seconds": _integer(
                source, "DLR_CACHE_SCAN_INTERVAL_SECONDS", 300, minimum=10, maximum=86_400
            ),
            "idle_ttl_seconds": _integer(
                source,
                "DLR_CACHE_IDLE_TTL_SECONDS",
                2_592_000,
                minimum=60,
                maximum=31_536_000,
            ),
            "min_idle_seconds": _integer(
                source,
                "DLR_CACHE_MIN_IDLE_SECONDS",
                86_400,
                minimum=0,
                maximum=31_536_000,
            ),
            "max_bytes": _integer(
                source, "DLR_CACHE_MAX_BYTES", 4_294_967_296, minimum=1, maximum=None
            ),
            "high_watermark_percent": _integer(
                source,
                "DLR_CACHE_HIGH_WATERMARK_PERCENT",
                85,
                minimum=1,
                maximum=99,
            ),
            "low_watermark_percent": _integer(
                source,
                "DLR_CACHE_LOW_WATERMARK_PERCENT",
                70,
                minimum=1,
                maximum=98,
            ),
            "disk_reserve_bytes": _integer(
                source,
                "DLR_CACHE_DISK_RESERVE_BYTES",
                134_217_728,
                minimum=0,
                maximum=None,
            ),
            "max_delete_bytes_per_round": _integer(
                source,
                "DLR_CACHE_MAX_DELETE_BYTES_PER_ROUND",
                268_435_456,
                minimum=1,
                maximum=None,
            ),
            "max_delete_entries_per_round": _integer(
                source,
                "DLR_CACHE_MAX_DELETE_ENTRIES_PER_ROUND",
                20,
                minimum=1,
                maximum=1000,
            ),
            "max_scan_entries_per_round": _integer(
                source,
                "DLR_CACHE_MAX_SCAN_ENTRIES_PER_ROUND",
                200,
                minimum=1,
                maximum=10_000,
            ),
            "max_scan_nodes_per_round": _integer(
                source,
                "DLR_CACHE_MAX_SCAN_NODES_PER_ROUND",
                100_000,
                minimum=1,
                maximum=1_000_000,
            ),
            "max_scan_hash_bytes_per_round": _integer(
                source,
                "DLR_CACHE_MAX_SCAN_HASH_BYTES_PER_ROUND",
                268_435_456,
                minimum=1,
                maximum=None,
            ),
            "max_scan_depth": _integer(
                source, "DLR_CACHE_MAX_SCAN_DEPTH", 64, minimum=1, maximum=256
            ),
            "max_round_seconds": _integer(
                source, "DLR_CACHE_MAX_ROUND_SECONDS", 10, minimum=1, maximum=60
            ),
            "staging_ttl_seconds": _integer(
                source,
                "DLR_CACHE_STAGING_TTL_SECONDS",
                86_400,
                minimum=60,
                maximum=31_536_000,
            ),
            "offline_protection": _boolean(source, "DLR_CACHE_OFFLINE_PROTECTION", True),
            "offline_mode": _boolean(source, "DLR_CACHE_OFFLINE_MODE", False),
            "shared_cache_mode": source.get("DLR_CACHE_SHARED_CACHE_MODE", "report_only"),
        }
        policy = cls(**values)
        if policy.min_idle_seconds > policy.idle_ttl_seconds:
            raise ValueError("DLR_CACHE_MIN_IDLE_SECONDS must not exceed IDLE_TTL_SECONDS")
        if not 0 < policy.low_watermark_percent < policy.high_watermark_percent < 100:
            raise ValueError("DLR_CACHE watermarks must satisfy 0 < low < high < 100")
        if policy.disk_reserve_bytes >= policy.max_bytes:
            raise ValueError("DLR_CACHE_DISK_RESERVE_BYTES must be less than MAX_BYTES")
        if policy.max_delete_bytes_per_round > policy.max_bytes:
            raise ValueError("DLR_CACHE_MAX_DELETE_BYTES_PER_ROUND must not exceed MAX_BYTES")
        if policy.shared_cache_mode != "report_only":
            raise ValueError("DLR_CACHE_SHARED_CACHE_MODE must be report_only")
        return policy

    def public_values(self) -> dict[str, Any]:
        return asdict(self)


PressureHandler = Callable[[int], bool]
_pressure_handlers: dict[str, PressureHandler] = {}
_handlers_lock = threading.Lock()


def _root_key(root: Path) -> str:
    return str(Path(root).absolute())


def managed_source_scope(language: str, source_url: str | None) -> str:
    """Return a non-sensitive stable relation for one configured package source."""

    normalized_language = language.strip().lower()
    if not normalized_language or len(normalized_language) > 32:
        raise ValueError("language must be a bounded identifier")
    source = "unconfigured"
    if source_url:
        parsed = urlsplit(source_url.strip())
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError as error:
            raise ValueError("source URL port is invalid") from error
        host = parsed.hostname.lower() if parsed.hostname else ""
        source = f"{parsed.scheme.lower()}://{host}{port}{parsed.path}"
    digest = hashlib.sha256(f"{normalized_language}\0{source}".encode()).hexdigest()
    return f"{normalized_language}:{digest}"


def register_pressure_handler(root: Path, handler: PressureHandler | None) -> None:
    with _handlers_lock:
        key = _root_key(root)
        if handler is None:
            _pressure_handlers.pop(key, None)
        else:
            _pressure_handlers[key] = handler


class GovernedVersionCache(VerifiedVersionCache):
    def __init__(self, root: Path, *, policy: CachePolicy) -> None:
        super().__init__(
            root,
            max_bytes=policy.max_bytes,
            low_watermark_bytes=policy.disk_reserve_bytes,
        )
        self.policy = policy

    def reserve(self, amount: int, *, ttl_seconds: int = 900) -> CacheReservation:
        try:
            return super().reserve(amount, ttl_seconds=ttl_seconds)
        except CacheError as error:
            if error.code != "cache_low_watermark" or not self.policy.pressure_gc_enabled:
                raise
        with _handlers_lock:
            handler = _pressure_handlers.get(_root_key(self.root))
        if handler is None or not handler(amount):
            raise CacheError("cache_no_safe_candidates")
        try:
            return super().reserve(amount, ttl_seconds=ttl_seconds)
        except CacheError as error:
            if error.code == "cache_low_watermark":
                raise CacheError("cache_capacity_insufficient") from error
            raise


def create_version_cache(
    runtime_root: Path, *, policy: CachePolicy | None = None
) -> GovernedVersionCache:
    effective = CachePolicy.from_environment() if policy is None else policy
    return GovernedVersionCache(Path(runtime_root) / "version-cache", policy=effective)
