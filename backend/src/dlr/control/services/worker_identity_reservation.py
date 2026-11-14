"""Unwired administrator-only creation of fresh reserved Worker identities.

No enrollment, cutover of existing Workers or route registration is performed.
The caller owns the enclosing transaction and may deliver its result after commit.
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import Worker
from dlr.control.models.worker_identity_reservation import WorkerIdentityReservation
from dlr.control.schemas.worker import _validate_name
from dlr.control.security import Principal
from dlr.control.services.adapter import domain_error

# Separate two-int advisory namespace from the Worker cutover lock.
_RESERVATION_REQUEST_LOCK_NAMESPACE = 0x444C5249


def _actor(principal: Principal) -> tuple[str, int | None]:
    if not isinstance(principal, Principal) or principal.must_change_password:
        raise domain_error(403, "account_admin_required", "Administrator privileges are required")
    if principal.kind == "superadmin" and principal.user_id is None:
        return "superadmin", None
    if (
        principal.kind == "account"
        and principal.role == "admin"
        and type(principal.user_id) is int
        and 0 < principal.user_id < 2**63
    ):
        return "account", principal.user_id
    raise domain_error(403, "account_admin_required", "Administrator privileges are required")


def reserve_identity(
    session: Session, *, name: object, request_id: object, principal: Principal
) -> WorkerIdentityReservation:
    """Create a new, disabled identity or replay the same administrator request."""
    actor_kind, actor_id = _actor(principal)
    if not isinstance(request_id, UUID):
        raise domain_error(422, "worker_identity_request_invalid", "Worker request is invalid")
    try:
        normalized_name = _validate_name(name)
    except ValueError:
        raise domain_error(422, "worker_identity_name_invalid", "Worker name is invalid") from None

    if session.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise domain_error(
            503,
            "worker_identity_isolation_unsupported",
            "Worker reservation requires read committed",
        )

    # Serialize equal request IDs even while their reservation row is absent.
    # Hash collisions only serialize unrelated requests, never authorize them.
    session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:request_id))"),
        {"namespace": _RESERVATION_REQUEST_LOCK_NAMESPACE, "request_id": str(request_id)},
    )
    existing = session.get(WorkerIdentityReservation, request_id)
    if existing is not None:
        if (
            existing.name != normalized_name
            or existing.actor_kind != actor_kind
            or existing.actor_id != actor_id
        ):
            raise domain_error(409, "worker_identity_request_conflict", "Worker request conflicts")
        return existing

    try:
        with session.begin_nested():
            worker = Worker(
                name=normalized_name,
                identity_lifecycle="reserved",
                status="offline",
                capabilities=[],
                protocol_version=4,
                scheduling_state="disabled",
                capacity_state="unknown",
            )
            session.add(worker)
            session.flush()
            reservation = WorkerIdentityReservation(
                request_id=request_id,
                worker_id=worker.id,
                name=normalized_name,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
            session.add(reservation)
            session.flush()
    except IntegrityError as error:
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_workers_name":
            raise domain_error(
                409, "worker_name_conflict", "Worker name is already in use"
            ) from None
        raise
    return reservation
