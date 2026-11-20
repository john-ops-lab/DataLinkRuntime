"""Durable storage capacity admission for the explicitly enrolled Control pair."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from dlr.common.config import Settings, settings
from dlr.common.schema_gate import try_control_gate
from dlr.control import db
from dlr.control.services import postgres_capacity_provider as capacity
from dlr.control.services.adapter import domain_error

if TYPE_CHECKING:
    from dlr.control.services.control_process_v4 import ControlProcess

logger = logging.getLogger(__name__)
_MAX_AGE = timedelta(seconds=15)
_MAX_FUTURE = timedelta(seconds=5)
_STORAGE_DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_local_process: ControlProcess | None = None


def set_local_process(process: ControlProcess | None) -> None:
    global _local_process
    _local_process = process


def fingerprint(config: Settings) -> str:
    from dlr.control.services.ingress_capacity import parse_profile

    if not config.postgres_capacity_url or not config.postgres_capacity_token:
        raise capacity.CapacityUnavailable("Capacity provider unavailable")
    profile = parse_profile(config.ingress_capacity_profile)
    contract = {
        "profile": profile.fingerprint,
        "provider": config.postgres_capacity_url,
        "replicas": 2,
        "http_requests": 128,
        "http_bytes": 8 * 1024**2,
        "source_owners": 16,
        "source_buffer_bytes": 64 * 1024**2,
        "intake_connections": 8,
        "management_connections": 4,
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _current(session: Session, *, lock: str = "") -> RowMapping:
    row = (
        session.execute(text("SELECT * FROM control_ha_capacity WHERE id=1" + lock))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise capacity.CapacityUnavailable("Capacity authority unavailable")
    return row


def _verified(session: Session, *, lock: str = "") -> RowMapping:
    process = _local_process
    if process is None or not process.capacity_compatible():
        raise capacity.CapacityUnavailable("Control capacity declaration unavailable")
    try_control_gate(session)
    process.assert_live(session)
    row = _current(session, lock=lock)
    if row["fingerprint"] != fingerprint(settings):
        raise capacity.CapacityUnavailable("Capacity configuration differs")
    return row


def _fresh(now: datetime, observed: datetime, recorded: datetime) -> bool:
    return -_MAX_FUTURE <= now - observed <= _MAX_AGE and timedelta(0) <= now - recorded <= _MAX_AGE


def storage_state(session: Session, *, for_accept: bool = False) -> str:
    """Read the current committed state, optionally locking to the T1 commit."""
    try:
        row = _verified(session, lock=" FOR SHARE" if for_accept else "")
        now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        if (
            now is None
            or row["observed_at"] is None
            or not _fresh(now, row["observed_at"], row["recorded_at"])
        ):
            return "unavailable"
        return str(row["state"])
    except Exception:
        return "unavailable"


def require_storage(session: Session) -> None:
    state = storage_state(session, for_accept=True)
    if state == "available":
        from dlr.control.services.rabbitmq import ingress_configuration_ready

        if not ingress_configuration_ready(session):
            raise domain_error(503, "rabbitmq_unavailable", "Broker admission unavailable")
        return
    if state == "low":
        raise domain_error(503, "ingress_disk_low", "Persistent storage is below low water")
    raise domain_error(
        503, "ingress_storage_capacity_unavailable", "PostgreSQL capacity observation unavailable"
    )


def admission_configured(session: Session) -> bool:
    try:
        _verified(session)
    except Exception:
        return False
    return True


@dataclass(frozen=True)
class _Probe:
    authority: capacity.VerifiedCapacityAuthority
    started_at: datetime


def _start_probe(session: Session) -> _Probe:
    current = _verified(session)
    cluster = session.scalar(
        text("SELECT cluster_id FROM control_cluster_authority WHERE singleton=1")
    )
    now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    if not isinstance(cluster, UUID) or now is None:
        raise capacity.CapacityUnavailable("Capacity authority unavailable")
    return _Probe(
        capacity.VerifiedCapacityAuthority(
            str(cluster), current["storage_domain_id"], current["topology_epoch"]
        ),
        now,
    )


def _record(
    session: Session, probe: _Probe, observation: capacity.CapacityObservation | None
) -> None:
    from dlr.control.services.ingress_capacity import DISK_RESUME_BYTES, DISK_STOP_BYTES

    row = _verified(session, lock=" FOR UPDATE")
    if (
        row["storage_domain_id"] != probe.authority.storage_domain_id
        or row["topology_epoch"] != probe.authority.topology_epoch
    ):
        return
    now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    if now is None or now < probe.started_at:
        raise capacity.CapacityUnavailable("Capacity authority unavailable")
    last_start = row["probe_started_at"]
    latest_probe = last_start is None or probe.started_at >= last_start
    previous = row["observed_at"]
    if (
        observation is None
        or not observation.usable(probe.authority)
        or not _fresh(now, observation.observed_at, probe.started_at)
    ):
        if latest_probe:
            session.execute(
                text("""
                    UPDATE control_ha_capacity SET state=CASE WHEN state='conflict'
                      THEN state ELSE 'unavailable' END, probe_started_at=:start WHERE id=1
                """),
                {"start": probe.started_at},
            )
        return
    measured, free = observation.observed_at, observation.writable_free_bytes
    if previous is not None and measured < previous:
        return
    if previous == measured and (row["state"] == "conflict" or row["writable_free_bytes"] != free):
        session.execute(
            text("UPDATE control_ha_capacity SET state='conflict', blocked=true WHERE id=1")
        )
        return
    if previous == measured and not latest_probe:
        return
    blocked = free <= DISK_STOP_BYTES or (row["blocked"] and free < DISK_RESUME_BYTES)
    # A slow earlier response must not reopen ingress after a newer failure.
    # A new low sample still takes effect and preserves hysteresis.
    state = "low" if blocked else "available" if latest_probe else "unavailable"
    session.execute(
        text("""
            UPDATE control_ha_capacity
            SET observed_at=:measured, recorded_at=:now, writable_free_bytes=:free,
                blocked=:blocked, state=:state, probe_started_at=:start WHERE id=1
        """),
        {
            "measured": measured,
            "now": now,
            "free": free,
            "blocked": blocked,
            "state": state,
            "start": probe.started_at if latest_probe else last_start,
        },
    )


def _poll(provider: capacity.PostgresCapacityProvider) -> bool:
    with db.SessionLocal() as session:
        probe = _start_probe(session)
    try:
        observation = provider.fetch(probe.authority)
    except capacity.CapacityUnavailable:
        observation = None
    with db.SessionLocal.begin() as session:
        _record(session, probe, observation)
    with db.SessionLocal() as session:
        return storage_state(session) == "available"


async def monitor() -> None:
    """With no valid local authority, retain management and leave intake closed."""
    try:
        provider = capacity.PostgresCapacityProvider(
            settings.postgres_capacity_url or "",
            settings.postgres_capacity_token or "",
            ca_file=settings.postgres_capacity_ca_file,
        )
    except (ValueError, OSError):
        logger.warning("PostgreSQL capacity provider configuration unavailable")
        return
    last_available = True
    while True:
        try:
            available = await asyncio.to_thread(_poll, provider)
        except Exception:
            available = False
        if available != last_available:
            logger.info("PostgreSQL capacity admission %s", "available" if available else "closed")
            last_available = available
        await asyncio.sleep(3)


def configure(session: Session, config: Settings, domain: str, epoch: int) -> dict[str, Any]:
    """Install a new capacity epoch offline without rewriting retained quota."""
    from dlr.control.models import IngressCapacityAudit, IngressCapacityConfiguration
    from dlr.control.services import control_cluster_identity, control_image_transition_v4
    from dlr.control.services.control_declaration_v4 import read_declaration
    from dlr.control.services.ingress_capacity import _lock_global_quota, parse_profile

    if _STORAGE_DOMAIN.fullmatch(domain) is None or not 0 < epoch < 2**63:
        raise capacity.CapacityUnavailable("Capacity configuration unavailable")
    profile = parse_profile(config.ingress_capacity_profile)
    contract = fingerprint(config)
    capacity.PostgresCapacityProvider(
        config.postgres_capacity_url or "",
        config.postgres_capacity_token or "",
        ca_file=config.postgres_capacity_ca_file,
    )
    try_control_gate(session)
    control_image_transition_v4._owner_fence(session, config)
    declaration = read_declaration(config)
    authority = control_cluster_identity._authority(session, declaration.authority, exclusive=True)
    if (
        authority["authority_epoch"] != declaration.authority_epoch
        or authority["capacity_fingerprint"] != declaration.authority.capacity_fingerprint
    ):
        raise capacity.CapacityUnavailable("Capacity configuration unavailable")
    _lock_global_quota(session)
    observed = (
        session.execute(text("SELECT * FROM control_ha_capacity WHERE id=1 FOR UPDATE"))
        .mappings()
        .one_or_none()
    )
    if observed is not None and epoch <= observed["topology_epoch"]:
        if (
            epoch == observed["topology_epoch"]
            and observed["storage_domain_id"] == domain
            and observed["fingerprint"] == contract
        ):
            return {"status": "verified", "epoch": epoch}
        raise capacity.CapacityUnavailable("Capacity epoch must advance")
    if (
        session.scalar(
            text("""
            SELECT EXISTS (SELECT 1 FROM control_process_instances
              WHERE NOT revoked AND lease_until > pg_catalog.clock_timestamp())
            OR EXISTS (SELECT 1 FROM subscription_leases
              WHERE lease_until > pg_catalog.clock_timestamp())
        """)
        )
        is not False
    ):
        raise capacity.CapacityUnavailable("Control or subscription leases are still live")
    previous = session.get(IngressCapacityConfiguration, 1, with_for_update=True)
    if previous is None:
        session.add(
            IngressCapacityConfiguration(
                id=1, fingerprint=profile.fingerprint, profile=profile.as_dict()
            )
        )
        old_fingerprint = None
    else:
        old_fingerprint = previous.fingerprint
        previous.fingerprint = profile.fingerprint
        previous.profile = profile.as_dict()
        previous.adopted_at = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    session.add(
        IngressCapacityAudit(
            previous_fingerprint=old_fingerprint,
            fingerprint=profile.fingerprint,
            profile=profile.as_dict(),
        )
    )
    session.execute(
        text("""
            INSERT INTO control_ha_capacity (id, fingerprint, storage_domain_id, topology_epoch)
            VALUES (1, :fingerprint, :domain, :epoch)
            ON CONFLICT (id) DO UPDATE SET fingerprint=EXCLUDED.fingerprint,
                storage_domain_id=EXCLUDED.storage_domain_id,
                topology_epoch=EXCLUDED.topology_epoch,
                observed_at=NULL, recorded_at=NULL, writable_free_bytes=NULL, probe_started_at=NULL,
                state='unavailable', blocked=true
        """),
        {"fingerprint": contract, "domain": domain, "epoch": epoch},
    )
    session.execute(text("UPDATE control_process_instances SET revoked=true WHERE NOT revoked"))
    return {"status": "configured", "epoch": epoch}
