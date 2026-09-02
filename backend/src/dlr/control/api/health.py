"""Health check endpoints of the Control Node."""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.services import attempt, outbox, rabbitmq

router = APIRouter()
logger = logging.getLogger("dlr.control.health")


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
def health() -> JSONResponse:
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
    return JSONResponse(content=payload, status_code=200 if healthy else 503)
