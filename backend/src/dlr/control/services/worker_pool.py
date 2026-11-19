"""Administrative Pool revisions and the Wave 2 routing boundary."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    SubscriptionActivation,
    Worker,
    WorkerEnvironmentClass,
    WorkerPool,
    WorkerPoolAudit,
    WorkerPoolMember,
    WorkerPoolRevision,
)
from dlr.control.schemas.worker_pool import (
    EnvironmentRule,
    FixedRoute,
    PlacementWaitPolicy,
    PoolAuditView,
    PoolCreate,
    PoolDetail,
    PoolMemberUpdate,
    PoolMemberView,
    PoolRevisionView,
    PoolRoute,
    PoolRule,
    PoolUpdate,
    PoolView,
    RouteSnapshot,
    RouteSource,
)
from dlr.control.security import Principal
from dlr.control.services.adapter import domain_error


def fixed_route(worker_id: int | None) -> FixedRoute | None:
    if worker_id is None:
        return None
    return FixedRoute(schema_version=1, mode="fixed", worker_id=worker_id, failover="none")


def adapter_route(adapter: Adapter) -> FixedRoute | PoolRoute | None:
    if adapter.pool_route is not None:
        return PoolRoute.model_validate(adapter.pool_route)
    return fixed_route(adapter.runtime_worker_id)


def require_fixed_route(route: FixedRoute | PoolRoute | None) -> None:
    if isinstance(route, PoolRoute):
        raise domain_error(409, "pool_routing_not_enabled", "Worker Pool execution is not enabled")


def _pool(session: Session, pool_id: int, *, lock: bool = False) -> WorkerPool:
    row = session.get(WorkerPool, pool_id, with_for_update=lock, populate_existing=lock)
    if row is None:
        raise domain_error(404, "worker_pool_not_found", "Worker Pool not found")
    return row


def _revision(session: Session, pool_id: int, revision: int) -> WorkerPoolRevision:
    row = session.get(WorkerPoolRevision, (pool_id, revision))
    if row is None:
        raise domain_error(404, "pool_revision_not_found", "Pool revision not found")
    return row


def _environment(session: Session, name: str) -> EnvironmentRule:
    row = session.get(WorkerEnvironmentClass, name)
    if row is None:
        raise domain_error(404, "worker_environment_not_found", "Worker environment not found")
    return EnvironmentRule.model_validate(row, from_attributes=True)


def pool_route(revision: WorkerPoolRevision) -> PoolRoute:
    return PoolRoute(
        schema_version=1,
        mode="pool",
        pool_id=revision.pool_id,
        pool_revision=revision.revision,
        selector=revision.selector,
        environment_class=revision.environment_class,
        placement_scope=revision.placement_scope,
        failover="compatible",
    )


def validate_pool_route(session: Session, route: PoolRoute) -> PoolRoute:
    # The configured object pins the revision. Current membership or capacity
    # is intentionally not copied into configuration or durable snapshots.
    revision = _revision(session, route.pool_id, route.pool_revision)
    normalized = pool_route(revision)
    if normalized != route:
        raise domain_error(
            409, "pool_route_revision_mismatch", "Pool route does not match revision"
        )
    return normalized


def snapshot_route(
    session: Session, route: FixedRoute | PoolRoute, language: str, source: RouteSource
) -> RouteSnapshot:
    environment: EnvironmentRule | str = "unknown"
    wait_policy: PlacementWaitPolicy | str = "w1-fixed"
    if isinstance(route, PoolRoute):
        validate_pool_route(session, route)
        environment = _environment(session, route.environment_class)
        wait_policy = PlacementWaitPolicy()
    return RouteSnapshot.model_validate(
        {
            "route": route,
            "source": source,
            "required_language": language,
            "environment": environment,
            "wait_policy": wait_policy,
        }
    )


def stored_snapshot(value: dict[str, object] | None) -> RouteSnapshot | None:
    return RouteSnapshot.model_validate(value) if value is not None else None


def activation_route(activation: SubscriptionActivation) -> FixedRoute | PoolRoute | None:
    snapshot = stored_snapshot(activation.route_snapshot)
    return snapshot.route if snapshot is not None else fixed_route(activation.worker_id)


def require_activation_worker(activation: SubscriptionActivation) -> int:
    route = activation_route(activation)
    require_fixed_route(route)
    worker_id = activation.worker_id
    if worker_id is None or not isinstance(route, FixedRoute) or route.worker_id != worker_id:
        raise domain_error(409, "activation_route_unavailable", "Frozen Worker is unavailable")
    return worker_id


def delivery_target(
    activation: SubscriptionActivation, *, use_worker_reservations: bool
) -> int | None:
    if (
        use_worker_reservations
        and activation.worker_id is None
        and isinstance(activation_route(activation), PoolRoute)
    ):
        return None
    return require_activation_worker(activation)


def activation_snapshot(
    session: Session, activation: SubscriptionActivation, language: str, source: RouteSource
) -> RouteSnapshot:
    snapshot = stored_snapshot(activation.route_snapshot)
    if snapshot is not None:
        return snapshot.model_copy(update={"source": source})
    route = fixed_route(activation.worker_id)
    if route is None:
        raise domain_error(409, "activation_route_unavailable", "Activation route is unavailable")
    return snapshot_route(session, route, language, source)


def _pool_view(pool: WorkerPool, revision: WorkerPoolRevision) -> PoolView:
    return PoolView(
        id=pool.id,
        name=pool.name,
        enabled=pool.enabled,
        version=pool.version,
        revision=PoolRevisionView.model_validate(revision),
    )


def list_pools(session: Session) -> list[PoolView]:
    rows = session.execute(
        select(WorkerPool, WorkerPoolRevision)
        .join(
            WorkerPoolRevision,
            (WorkerPoolRevision.pool_id == WorkerPool.id)
            & (WorkerPoolRevision.revision == WorkerPool.current_revision),
        )
        .order_by(WorkerPool.name, WorkerPool.id)
    )
    return [_pool_view(pool, revision) for pool, revision in rows]


def pool_detail(session: Session, pool_id: int) -> PoolDetail:
    pool = _pool(session, pool_id)
    view = _pool_view(pool, _revision(session, pool_id, pool.current_revision))
    revisions = session.scalars(
        select(WorkerPoolRevision)
        .where(WorkerPoolRevision.pool_id == pool_id)
        .order_by(WorkerPoolRevision.revision.desc())
    )
    members = session.scalars(
        select(WorkerPoolMember)
        .where(WorkerPoolMember.pool_id == pool_id)
        .order_by(WorkerPoolMember.worker_id)
    )
    audit = session.scalars(
        select(WorkerPoolAudit)
        .where(WorkerPoolAudit.pool_id == pool_id)
        .order_by(WorkerPoolAudit.id.desc())
        .limit(50)
    )
    return PoolDetail(
        **view.model_dump(),
        revisions=[PoolRevisionView.model_validate(row) for row in revisions],
        members=[PoolMemberView.model_validate(row) for row in members],
        audit=[PoolAuditView.model_validate(row) for row in audit],
    )


def list_environments(session: Session) -> list[EnvironmentRule]:
    return [
        EnvironmentRule.model_validate(row, from_attributes=True)
        for row in session.scalars(
            select(WorkerEnvironmentClass).order_by(WorkerEnvironmentClass.name)
        )
    ]


def create_environment(session: Session, rule: EnvironmentRule) -> EnvironmentRule:
    session.add(WorkerEnvironmentClass(**rule.model_dump()))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409, "worker_environment_conflict", "A Worker environment with this name already exists"
        ) from None
    return rule


def _audit(
    session: Session,
    pool: WorkerPool,
    principal: Principal,
    action: str,
    details: dict[str, object],
) -> None:
    session.add(
        WorkerPoolAudit(
            pool_id=pool.id,
            version=pool.version,
            actor_kind=principal.kind,
            actor_user_id=principal.user_id,
            action=action,
            details=details,
        )
    )


def _append_revision(
    session: Session, pool: WorkerPool, revision: int, rule: PoolRule
) -> WorkerPoolRevision:
    _environment(session, rule.environment_class)
    row = WorkerPoolRevision(pool_id=pool.id, revision=revision, **rule.model_dump())
    session.add(row)
    return row


def _expect_version(pool: WorkerPool, expected: int) -> None:
    if pool.version != expected:
        raise domain_error(
            409,
            "worker_pool_version_conflict",
            "Worker Pool configuration has changed",
            {"current_version": pool.version},
        )


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "worker_pool_conflict", "Worker Pool update conflicts") from None


def create_pool(session: Session, payload: PoolCreate, principal: Principal) -> PoolDetail:
    pool = WorkerPool(name=payload.name, version=1, current_revision=1)
    session.add(pool)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise domain_error(409, "worker_pool_conflict", "Worker Pool name already exists") from None
    _append_revision(session, pool, 1, payload.rule)
    _audit(session, pool, principal, "create", {"revision": 1})
    _commit(session)
    return pool_detail(session, pool.id)


def update_pool(
    session: Session, pool_id: int, payload: PoolUpdate, principal: Principal
) -> PoolDetail:
    pool = _pool(session, pool_id, lock=True)
    _expect_version(pool, payload.expected_version)
    before: dict[str, object] = {
        "name": pool.name,
        "enabled": pool.enabled,
        "revision": pool.current_revision,
    }
    if payload.rule is not None:
        current = _revision(session, pool_id, pool.current_revision)
        if PoolRule.model_validate(current, from_attributes=True) != payload.rule:
            next_revision = pool.current_revision + 1
            _append_revision(session, pool, next_revision, payload.rule)
            pool.current_revision = next_revision
    if payload.name is not None:
        pool.name = payload.name
    if payload.enabled is not None:
        pool.enabled = payload.enabled
    after: dict[str, object] = {
        "name": pool.name,
        "enabled": pool.enabled,
        "revision": pool.current_revision,
    }
    if after != before:
        pool.version += 1
        _audit(session, pool, principal, "update", {"before": before, "after": after})
    _commit(session)
    return pool_detail(session, pool.id)


def update_member(
    session: Session, pool_id: int, worker_id: int, payload: PoolMemberUpdate, principal: Principal
) -> PoolDetail:
    pool = _pool(session, pool_id, lock=True)
    _expect_version(pool, payload.expected_version)
    worker = session.get(Worker, worker_id)
    if worker is None or payload.worker_identity_id != worker.identity_id:
        raise domain_error(409, "pool_worker_identity_mismatch", "Worker identity does not match")
    if payload.enabled and (
        worker.protocol_version != 4 or worker.identity_lifecycle != "reserved"
    ):
        raise domain_error(409, "pool_worker_not_v4", "Pool membership requires a v4 Worker")
    member = session.get(WorkerPoolMember, (pool_id, worker_id))
    before = PoolMemberView.model_validate(member).model_dump(mode="json") if member else None
    changes = payload.model_dump(exclude={"expected_version", "worker_identity_id"})
    if member is None:
        member = WorkerPoolMember(
            pool_id=pool_id, worker_id=worker_id, worker_identity_id=worker.identity_id, **changes
        )
        session.add(member)
    else:
        for key, value in changes.items():
            setattr(member, key, value)
    after = PoolMemberView.model_validate(member).model_dump(mode="json")
    if before != after:
        pool.version += 1
        _audit(session, pool, principal, "member", {"before": before, "after": after})
    _commit(session)
    return pool_detail(session, pool.id)


def delete_revision(
    session: Session, pool_id: int, revision: int, expected_version: int, principal: Principal
) -> PoolDetail:
    pool = _pool(session, pool_id, lock=True)
    _expect_version(pool, expected_version)
    if pool.current_revision == revision:
        raise domain_error(
            409, "pool_revision_in_use", "The active Pool revision cannot be deleted"
        )
    session.delete(_revision(session, pool_id, revision))
    pool.version += 1
    _audit(session, pool, principal, "delete_revision", {"revision": revision})
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409, "pool_revision_in_use", "A referenced Pool revision cannot be deleted"
        ) from None
    return pool_detail(session, pool.id)


def member_rule_reason(
    pool: WorkerPool,
    revision: WorkerPoolRevision,
    worker: Worker,
    member: WorkerPoolMember | None,
    *,
    expected_environment: EnvironmentRule,
    verified_environment: EnvironmentRule | None,
    language: str,
    credential_ids: frozenset[int] = frozenset(),
    material_scopes: frozenset[str] = frozenset(),
) -> str | None:
    """Match administrative grants to independently verified environment facts."""
    if not pool.enabled:
        return "worker_pool_disabled"
    if (
        member is None
        or not member.enabled
        or member.pool_id != pool.id
        or revision.pool_id != pool.id
        or member.worker_id != worker.id
        or member.worker_identity_id != worker.identity_id
        or worker.protocol_version != 4
        or worker.identity_lifecycle != "reserved"
    ):
        return "pool_membership_unavailable"
    if (
        revision.placement_scope not in member.placement_scopes
        or any(member.labels.get(k) != v for k, v in revision.selector.items())
        or not credential_ids.issubset(member.credential_ids)
        or not material_scopes.issubset(member.material_scopes)
    ):
        return "pool_placement_not_authorized"
    if verified_environment is None:
        return "worker_environment_unknown"
    if (
        expected_environment.name != revision.environment_class
        or verified_environment.image_digest != expected_environment.image_digest
        or verified_environment.architecture != expected_environment.architecture
        or language not in worker.capabilities
        or language not in expected_environment.toolchains
        or expected_environment.toolchains.get(language)
        != verified_environment.toolchains.get(language)
        or not set(expected_environment.builtin_materials).issubset(
            verified_environment.builtin_materials
        )
    ):
        return "worker_environment_incompatible"
    return None


def preview_members(
    session: Session, pool_id: int, revision_number: int, language: str
) -> list[dict[str, object]]:
    pool = _pool(session, pool_id)
    revision = _revision(session, pool_id, revision_number)
    expected = _environment(session, revision.environment_class)
    rows = session.execute(
        select(Worker, WorkerPoolMember)
        .join(WorkerPoolMember, WorkerPoolMember.worker_id == Worker.id)
        .where(WorkerPoolMember.pool_id == pool_id)
        .order_by(Worker.id)
    )
    # W1 observations are self-reported and cannot prove the deployed image
    # or scope. Capacity/attestation integration is required for live placement.
    return [
        {
            "worker_id": worker.id,
            "reason": member_rule_reason(
                pool,
                revision,
                worker,
                member,
                expected_environment=expected,
                verified_environment=None,
                language=language,
            ),
        }
        for worker, member in rows
    ]
