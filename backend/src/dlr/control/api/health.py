"""Health check endpoints of the Control Node."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.schema_gate import try_control_gate
from dlr.common.schema_version import SCHEMA_REVISION
from dlr.control import db
from dlr.control.services import attempt, outbox, rabbitmq

router = APIRouter()
logger = logging.getLogger("dlr.control.health")
_NO_CACHE = {"Cache-Control": "no-store"}


@router.get("/api/health/live")
def live() -> JSONResponse:
    return JSONResponse({"status": "live"}, headers=_NO_CACHE)


@router.get("/api/health/ready")
def ready(request: Request) -> JSONResponse:
    """Check core transaction authority independently of Broker and storage."""
    available = False
    try:
        with db.SessionLocal.begin() as session:
            # A standby/read-only login can answer SELECT 1 but cannot
            # accept responsibility. Check the normal transaction mode and
            # the actual schema and lease, including transaction completion.
            if session.scalar(text("SHOW transaction_read_only")) != "off":
                raise ValueError("read-only database transaction")
            try_control_gate(session)
            session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            if session.execute(text("SELECT version_num FROM alembic_version")).scalars().all() != [
                SCHEMA_REVISION
            ]:
                raise ValueError("incompatible schema")
            if settings.worker_remote_v4_enabled:
                process = getattr(request.app.state, "control_process_v4", None)
                if process is None:
                    raise ValueError("Control authority unavailable")
                process.assert_live(session)
                if settings.control_ha_slot is not None:
                    from dlr.control.services.worker_token_fence_v4 import _database_fence

                    _database_fence(session)
        available = True
    except Exception:
        # Failed probes are ordinary during maintenance; no backend exception
        # or configuration values belong in this public response or log.
        pass
    return JSONResponse(
        {"service": "dlr-control", "status": "ready" if available else "not_ready"},
        status_code=200 if available else 503,
        headers=_NO_CACHE,
    )


def _repair_health_ok(rabbitmq_status: dict[str, object], outbox_status: dict[str, object]) -> bool:
    """Keep Broker repair responsibility separate from the ingress gate.

    A fresh configured deployment with no Worker and no pending Outbox has no
    Broker responsibility to repair yet, so ``waiting_for_worker`` is healthy
    enough for the Worker dependency cycle to start.  The same state is not
    healthy once a pending row exists, and no arbitrary degraded state is
    treated as healthy.
    """
    repair_status = rabbitmq_status.get("repair", {})
    if not isinstance(repair_status, dict) or outbox_status.get("status") != "ok":
        return False
    if repair_status.get("status") in {"disabled", "ready"}:
        return True
    if repair_status.get("status") != "waiting_for_worker":
        return False
    return (
        repair_status.get("worker_count") == 0
        and repair_status.get("last_error_code") is None
        and outbox_status.get("pending_count") == 0
    )


def read_outbox_health(database_ok: bool, *, session: Session | None = None) -> dict[str, object]:
    """Read authoritative pending Outbox facts without exposing connection data."""
    if not database_ok:
        return {
            "status": "unavailable",
            "pending_count": None,
            "pending_bytes": None,
            "oldest_age_seconds": None,
            "error_code": "database_unavailable",
        }
    try:
        if session is not None:
            return outbox.backlog_health(session)
        with db.SessionLocal() as owned_session:
            return outbox.backlog_health(owned_session)
    except Exception:  # noqa: BLE001 - health must return a stable payload
        logger.warning("outbox health query failed; code=outbox_backlog_unavailable")
        return {
            "status": "unavailable",
            "pending_count": None,
            "pending_bytes": None,
            "oldest_age_seconds": None,
            "error_code": "outbox_backlog_unavailable",
        }


@router.get("/api/health")
def health(request: Request) -> JSONResponse:
    """Report Control, ingress, repair and Outbox health without secrets."""
    database_ok = db.check_database()
    if database_ok:
        with db.SessionLocal() as session:
            rabbitmq_status = rabbitmq.runtime_health(session)
            outbox_status = read_outbox_health(database_ok, session=session)
    else:
        rabbitmq_status = rabbitmq.runtime_health()
        outbox_status = read_outbox_health(database_ok)
    pending_count = outbox_status.get("pending_count")
    if not settings.rabbitmq_url and isinstance(pending_count, int) and pending_count > 0:
        # A disabled ingress is normal only while there is no RabbitMQ
        # responsibility to repair.  Once a pending Outbox exists, hiding the
        # missing repair URL behind the ingress flag would lose the durable
        # responsibility boundary.
        existing_repair = rabbitmq_status.get("repair")
        repair_payload = existing_repair if isinstance(existing_repair, dict) else {}
        rabbitmq_status = {
            **rabbitmq_status,
            "status": "degraded",
            "last_error_code": "rabbitmq_not_configured_for_pending",
            "repair": {
                **repair_payload,
                "status": "degraded",
                "ready": False,
                "last_error_code": "rabbitmq_not_configured_for_pending",
            },
        }
    repair_ok = _repair_health_ok(rabbitmq_status, outbox_status)
    # Ingress readiness is intentionally reported separately.  A fresh
    # gate-on deployment may be healthy while waiting for its fixed Worker;
    # accepting RabbitMQ Executions still requires ingress_configuration_ready.
    rabbitmq_ok = repair_ok
    outbox_ok = outbox_status["status"] == "ok"
    healthy = database_ok and rabbitmq_ok and outbox_ok
    payload = {
        "service": "dlr-control",
        "status": "ok" if healthy else "degraded",
        "database": database_ok,
        "rabbitmq": rabbitmq_status,
        "outbox": outbox_status,
        "reliable_runtime": {"attempt": attempt.metrics_snapshot()},
    }
    if settings.control_ha_slot is not None:
        payload["control_ha"] = _cluster_health(request, rabbitmq_status, database_ok)
    return JSONResponse(content=payload, status_code=200 if healthy else 503)


def _cluster_health(
    request: Request, rabbitmq_status: dict[str, object], database_ok: bool
) -> dict[str, object]:
    from dlr.control.services import control_ha_capacity

    process = getattr(request.app.state, "control_process_v4", None)
    compatibility = None
    registered: int | None = None
    storage = "unavailable"
    capacity_compatible: bool | None = None
    if database_ok and process is not None:
        try:
            with db.SessionLocal() as session:
                try_control_gate(session)
                process.assert_live(session)
                compatibility = process.compatibility()
                registered = session.scalar(
                    text("""
                        SELECT count(DISTINCT ha_slot) FROM control_process_instances
                        WHERE NOT revoked AND lease_until > pg_catalog.clock_timestamp()
                          AND authority_epoch = :epoch AND ha_slot IS NOT NULL
                    """),
                    {"epoch": process._deployment.authority_epoch},
                )
                storage, capacity_compatible = control_ha_capacity.storage_state(session)
        except Exception:
            compatibility = None
    ingress = rabbitmq_status.get("ingress")
    repair = rabbitmq_status.get("repair")
    ingress_ready = isinstance(ingress, dict) and ingress.get("ready") is True
    publish_ready = isinstance(repair, dict) and repair.get("ready") is True
    return {
        "node": {
            "slot": settings.control_ha_slot,
            "registered": compatibility is not None,
            "capacity_compatible": capacity_compatible,
        },
        "cluster": {"registered": registered, "max_nodes": 2},
        "features": {
            "new_ingress": "available"
            if storage == "available" and ingress_ready
            else "unavailable",
            "postgres_capacity": storage,
            "mq_publish": "available" if publish_ready else "unavailable",
            "managed_files": "awaiting_shared_backend",
            "builtin_packages": "awaiting_shared_backend",
            "ai_attachments": "awaiting_shared_backend",
        },
    }
