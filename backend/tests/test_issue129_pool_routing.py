"""Wave 2 routing drafts, admission boundaries and held historical facts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from fastapi import HTTPException
from sqlalchemy import MetaData, Table, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.schema_version import SCHEMA_REVISION
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    AdapterVersion,
    DeliveryDisposition,
    EventDelivery,
    EventSubscription,
    Execution,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionQuota,
    SyncBatch,
    Worker,
    WorkerPool,
    WorkerPoolMember,
    WorkerPoolRevision,
)
from dlr.control.security import SUPERADMIN_PRINCIPAL, Principal, require_principal
from dlr.control.services import attempt, outbox, worker_pool
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services import subscription_sync as sync
from dlr.control.services.control_image_transition_v4 import W1_SCHEMA
from runtime_api_support import WORKER_HEADERS, claim_execution, ready_registration, report_attempt
from test_adapters import STARTER_CODE, create_adapter, save_version
from test_issue219_delivery import delivery_id, receive
from test_issue219_delivery import world as world
from test_issue219_management import action, config
from test_issue219_sync import kube as kube
from test_issue219_upgrade import insert_legacy_adapter, migration_config
from test_issue219_upgrade import upgrade_database as upgrade_database
from test_webhook_trigger import put_webhook, setup_webhook


def assert_rejected(response, code="pool_routing_not_enabled"):
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code


@pytest.fixture
def pool_setup(api_client):
    environment = {
        "name": "py313-amd64-r1",
        "image_digest": "sha256:" + "a" * 64,
        "architecture": "amd64",
        "toolchains": {"python": "b" * 64},
        "builtin_materials": ["wheelhouse"],
    }
    rule = {
        "environment_class": environment["name"],
        "placement_scope": "trusted",
        "selector": {"zone": "a"},
    }
    response = api_client.post("/api/worker-environments", json=environment)
    assert response.status_code == 201, response.text
    response = api_client.post("/api/worker-pools", json={"name": "Heavy", "rule": rule})
    assert response.status_code == 201, response.text
    pool = response.json()
    route = {
        "schema_version": 1,
        "mode": "pool",
        "pool_id": pool["id"],
        "pool_revision": 1,
        "failover": "compatible",
        **rule,
    }
    return pool, route, environment


def bind_draft(client, adapter_id, route):
    path = f"/api/adapters/{adapter_id}"
    version = client.get(path).json()["route_version"]
    response = client.patch(path, json={"expected_route_version": version, "route": route})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["runtime_worker_id"] is None and result["route"] == route
    assert result["route_version"] == version + 1
    assert result["runtime_locked"] is False
    return result


def register(client, name):
    response = client.post(
        "/api/workers/register", json=ready_registration(name, ["python"]), headers=WORKER_HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_pool_draft_fails_before_new_responsibility_and_legacy_fixed_still_works(
    api_client, session_factory, pool_setup, monkeypatch
):
    pool, route, _ = pool_setup
    app = api_client.app
    app.dependency_overrides[require_principal] = lambda: Principal(kind="account", role="user")
    try:
        assert api_client.get("/api/worker-pools").status_code == 200
        assert api_client.get(f"/api/worker-pools/{pool['id']}").status_code == 403
        assert api_client.post("/api/worker-pools", json={}).status_code == 403
    finally:
        del app.dependency_overrides[require_principal]

    fixed_worker = register(api_client, "fixed-route-node")
    adapter = create_adapter(api_client)
    adapter_id = adapter["id"]
    path = f"/api/adapters/{adapter_id}"
    conflict = api_client.patch(
        path,
        json={
            "route": route,
            "expected_route_version": adapter["route_version"],
            "runtime_worker_id": fixed_worker,
            "description": "must roll back",
        },
    )
    assert conflict.status_code == 422
    assert api_client.get(path).json()["description"] == ""
    draft = bind_draft(api_client, adapter_id, route)
    assert api_client.post(path + "/versions", json={"code": STARTER_CODE}).status_code == 201
    assert api_client.patch(path, json={"runtime_worker_id": None}).json()["route"] == route
    assert_rejected(api_client.post(path + "/executions", json={}))

    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    monkeypatch.setattr(management, "AVAILABLE_SOURCES", frozenset({"kafka"}))
    response = api_client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": adapter_id,
            "name": "pool-draft",
            "config": config(),
        },
    )
    assert response.status_code == 201, response.text
    assert_rejected(action(api_client, response.json(), "enable"))

    scheduled = create_adapter(api_client, "scheduled-draft")
    schedule_path = f"/api/adapters/{scheduled['id']}"
    bind_draft(api_client, scheduled["id"], route)
    assert (
        api_client.post(schedule_path + "/versions", json={"code": STARTER_CODE}).status_code == 201
    )
    assert api_client.patch(schedule_path, json={"run_mode": "schedule"}).status_code == 200
    schedule = {"cron": "0 * * * *", "timezone": "UTC"}
    assert (
        api_client.put(schedule_path + "/schedule", json={**schedule, "enabled": False}).status_code
        == 200
    )
    assert_rejected(api_client.put(schedule_path + "/schedule", json={**schedule, "enabled": True}))

    hook, _, _, credential, _ = setup_webhook(api_client, "stopped-hook", enabled=False)
    bind_draft(api_client, hook["id"], route)
    assert_rejected(put_webhook(api_client, hook["id"], credential["id"]))
    with session_factory() as session:
        for model in (
            Execution,
            ExecutionOutbox,
            AdapterExecutionAdmission,
            SubscriptionActivation,
            EventDelivery,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert_rejected(
        api_client.patch(
            path,
            json={
                "expected_route_version": draft["route_version"] - 1,
                "runtime_worker_id": fixed_worker,
            },
        ),
        "adapter_route_version_conflict",
    )
    legacy = api_client.patch(path, json={"runtime_worker_id": fixed_worker})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["route"] == worker_pool.fixed_route(fixed_worker).model_dump()


def test_member_authorization_and_old_revision_remain_independent_of_new_rule(
    api_client, session_factory, pool_setup
):
    pool, route, _ = pool_setup
    base = f"/api/worker-pools/{pool['id']}"
    adapter = create_adapter(api_client)
    bind_draft(api_client, adapter["id"], route)
    with session_factory.begin() as session:
        node = Worker(
            name="member",
            status="offline",
            protocol_version=4,
            identity_lifecycle="reserved",
            capabilities=["python"],
        )
        session.add(node)
        session.flush()
        node_id, identity = node.id, str(node.identity_id)
    updated = api_client.patch(
        base,
        json={
            "expected_version": pool["version"],
            "rule": {
                "environment_class": route["environment_class"],
                "placement_scope": "trusted",
                "selector": {"zone": "b"},
            },
        },
    ).json()
    assert updated["revision"]["revision"] == 2
    assert_rejected(
        api_client.delete(
            base + "/revisions/1",
            params={
                "expected_version": updated["version"],
            },
        ),
        "pool_revision_in_use",
    )
    member_path = base + f"/members/{node_id}"
    grants = {
        "worker_identity_id": identity,
        "enabled": True,
        "labels": {"zone": "a"},
        "placement_scopes": ["trusted"],
        "credential_ids": [23],
        "material_scopes": ["wheelhouse"],
    }
    response = api_client.put(member_path, json={**grants, "expected_version": updated["version"]})
    assert response.status_code == 200, response.text
    populated = response.json()
    assert api_client.get(base + "/revisions/1/member-preview?language=python").json() == [
        {"worker_id": node_id, "reason": "worker_environment_unknown"},
    ]
    assert api_client.get(base + "/revisions/2/member-preview?language=python").json()[0][
        "reason"
    ] == ("pool_placement_not_authorized")
    with session_factory() as session:
        row = session.get(WorkerPool, pool["id"])
        node = session.get(Worker, node_id)
        revision = session.get(WorkerPoolRevision, (row.id, 1))
        member = session.get(WorkerPoolMember, (row.id, node_id))
        snapshot = worker_pool.snapshot_route(
            session, worker_pool.pool_route(revision), "python", "adapter"
        )
        assert (
            snapshot.route.pool_revision == 1
            and snapshot.wait_policy.placement_wait_timeout_seconds == 900
        )
        facts = {
            "expected_environment": snapshot.environment,
            "verified_environment": snapshot.environment,
            "language": "python",
            "material_scopes": frozenset({"wheelhouse"}),
        }
        assert (
            worker_pool.member_rule_reason(
                row, revision, node, member, credential_ids=frozenset({23}), **facts
            )
            is None
        )
        assert worker_pool.member_rule_reason(
            row, revision, node, member, credential_ids=frozenset({24}), **facts
        ) == ("pool_placement_not_authorized")
    revoked = api_client.put(
        member_path, json={**grants, "enabled": False, "expected_version": populated["version"]}
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["audit"][0]["action"] == "member"
    assert api_client.get(base + "/revisions/1/member-preview?language=python").json()[0][
        "reason"
    ] == ("pool_membership_unavailable")


def test_route_write_rolls_back_when_an_unreferenced_pool_revision_is_deleted(
    api_client, session_factory, pool_setup, monkeypatch
):
    pool, route, _ = pool_setup
    changed = api_client.patch(
        f"/api/worker-pools/{pool['id']}",
        json={
            "expected_version": pool["version"],
            "rule": {
                "environment_class": route["environment_class"],
                "placement_scope": route["placement_scope"],
                "selector": {"zone": "b"},
            },
        },
    ).json()
    adapter = create_adapter(api_client)
    validate = worker_pool.validate_pool_route

    def delete_after_read(session, selection):
        result = validate(session, selection)
        with session_factory() as deleting:
            worker_pool.delete_revision(
                deleting, pool["id"], 1, changed["version"], SUPERADMIN_PRINCIPAL
            )
        return result

    monkeypatch.setattr(worker_pool, "validate_pool_route", delete_after_read)
    path = f"/api/adapters/{adapter['id']}"
    assert_rejected(
        api_client.patch(
            path,
            json={
                "route": route,
                "expected_route_version": adapter["route_version"],
                "name": "mixed edit",
                "run_mode": "schedule",
                "timeout_seconds": 20,
            },
        ),
        "pool_route_revision_unavailable",
    )
    after = api_client.get(path).json()
    for key in ("route", "route_version", "name", "run_mode", "timeout_seconds"):
        assert after[key] == adapter[key]


@pytest.mark.parametrize("checkpoint_exists", [False, True])
def test_kubernetes_pool_draft_reserves_no_sync_batch_or_checkpoint(
    kube, session_factory, pool_setup, checkpoint_exists
):
    _, route, _ = pool_setup
    with session_factory.begin() as session:
        initial = session.get(SubscriptionActivation, kube.activation_id)
        revision = session.get(WorkerPoolRevision, (route["pool_id"], route["pool_revision"]))
        frozen = worker_pool.snapshot_route(
            session, worker_pool.pool_route(revision), "python", "adapter"
        )
        activation = SubscriptionActivation(
            subscription_id=initial.subscription_id,
            config_version_id=initial.config_version_id,
            revision_id=initial.revision_id,
            route_snapshot=frozen.model_dump(mode="json"),
        )
        session.add(activation)
        session.flush()
        subscription = session.get(EventSubscription, initial.subscription_id)
        subscription.activation_id = activation.id
        context = replace(kube, activation_id=activation.id)
        if checkpoint_exists:
            session.add(
                SourceCheckpoint(
                    subscription_id=subscription.id,
                    source_epoch=subscription.source_epoch,
                    shard=sync.KUBERNETES_SHARD,
                    position={},
                    confirmed_position={},
                    next_sequence=1,
                    assignment_generation=1,
                )
            )
    with session_factory.begin() as session:
        with pytest.raises(HTTPException) as rejected:
            sync.begin_sync_batch(session, context, context.identity_snapshot)
        assert rejected.value.detail["code"] == "pool_routing_not_enabled"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SyncBatch)) == 0
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == int(
            checkpoint_exists
        )
        quota = session.get(SubscriptionQuota, "global")
        assert quota is None or (quota.reserved_control_count == quota.reserved_control_bytes == 0)


def test_old_manual_idempotency_and_replay_keep_distinct_fixed_workers(
    api_client, session_factory, pool_setup
):
    _, route, _ = pool_setup
    first = register(api_client, "fixed-original")
    second = register(api_client, "fixed-replay")
    adapter = create_adapter(api_client)
    adapter_path = f"/api/adapters/{adapter['id']}"
    assert api_client.patch(adapter_path, json={"runtime_worker_id": first}).status_code == 200
    save_version(api_client, adapter["id"])
    ingress = adapter_path + "/executions"
    headers = {"Idempotency-Key": "original-request"}
    accepted = api_client.post(ingress, json={}, headers=headers)
    assert accepted.status_code == 202, accepted.text
    original = accepted.json()
    assert original["route_snapshot"]["route"]["worker_id"] == first
    claim = claim_execution(api_client, first, execution_id=original["id"])
    assert claim.status_code == 200, claim.text
    reported = report_attempt(
        api_client,
        first,
        original["id"],
        {
            "status": "failed",
            "error_code": "business_error",
            "error": "fixture",
        },
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["status"] == "dead_letter"
    bind_draft(api_client, adapter["id"], route)
    assert api_client.post(ingress, json={}, headers=headers).json()["id"] == original["id"]
    assert_rejected(api_client.post(ingress, json={}))
    replay_path = f"/api/executions/{original['id']}/replay"
    assert_rejected(api_client.post(replay_path))
    assert api_client.patch(adapter_path, json={"runtime_worker_id": second}).status_code == 200
    replay = api_client.post(replay_path)
    assert replay.status_code == 200, replay.text
    assert replay.json()["route_snapshot"]["source"] == "current-config"
    assert replay.json()["route_snapshot"]["route"]["worker_id"] == second
    with session_factory() as session:
        saved = session.get(Execution, original["id"])
        assert saved.route_snapshot["route"]["worker_id"] == saved.worker_id == first
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 2


def test_event_t2_and_original_replay_do_not_read_the_current_pool_draft(
    world, session_factory, pool_setup
):
    _, route, _ = pool_setup
    with session_factory.begin() as session:
        receipt = receive(session, world)
        receipt_id = receipt.id
        original_delivery = delivery_id(session, receipt)
        adapter = session.get(Adapter, world["adapter"])
        fixed_id = adapter.runtime_worker_id
        adapter.runtime_worker_id, adapter.pool_route = None, route
        execution = delivery.dispatch_delivery(session, original_delivery)
        assert execution.route_snapshot["route"]["worker_id"] == fixed_id
        assert execution.route_snapshot["source"] == "activation"
        execution.status = "succeeded"
        delivery.observe_terminal(session, original_delivery)
        sub = session.get(SubscriptionActivation, receipt.activation_id)
        session.get(Adapter, world["adapter"]).route_version += 1
        subscription = session.get(EventSubscription, receipt.subscription_id)
        subscription.desired_state = "disabled"
        context, token = delivery.current_replay_context(session, subscription)
        assert sub.worker_id == fixed_id and context["worker_id"] is None
        assert context["route_snapshot"]["route"] == route
        with pytest.raises(HTTPException) as rejected:
            delivery.replay_event(
                session,
                receipt_id,
                "pool-current",
                None,
                "fixture",
                use_current_configuration=True,
                expected_preview_token=token,
            )
        assert rejected.value.detail["code"] == "pool_routing_not_enabled"
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 0
        assert session.scalar(select(func.count()).select_from(SubscriptionActivation)) == 1
        original = delivery.replay_event(session, receipt_id, "original", None, "fixture")
        assert (
            delivery.replay_event(session, receipt_id, "original", None, "fixture").id
            == original.id
        )
        replayed = delivery.dispatch_delivery(session, original.id)
        assert replayed.route_snapshot["source"] == "original"
        assert replayed.target_worker_id_snapshot == fixed_id


def test_pool_asset_rebinding_and_isolated_snapshot_cannot_dispatch(
    api_client, session_factory, pool_setup
):
    _, route, _ = pool_setup
    adapter = create_adapter(api_client)
    path = f"/api/adapters/{adapter['id']}"
    bind_draft(api_client, adapter["id"], route)
    version = api_client.post(path + "/versions", json={"code": STARTER_CODE}).json()
    preview = api_client.post(path + "/portable-preview", json={}).json()
    assert preview["runtime_route_requires_rebind"] is True
    assert set(preview).isdisjoint({"route", "pool_id", "runtime_worker_id"})
    target = api_client.post("/api/portable/adapters", json={"package": preview})
    clone = api_client.post(path + "/clone", json={"name": "rebinding-clone"})
    template = api_client.post(path + "/portable-preview", json={"as_template": True}).json()
    template.pop("runtime_route_requires_rebind")
    saved = api_client.post(path + "/templates", json={"package": template})
    assert saved.status_code == 201, saved.text
    copy = api_client.post(
        f"/api/templates/scenarios/{saved.json()['slug']}/variants/python/instantiate",
        json={
            "name": "rebinding-template",
            "expected_template_version": saved.json()["template_version"],
        },
    )
    for result in (target, clone, copy):
        assert result.status_code == 201, result.text
        assert result.json()["route"] is None
        assert result.json()["configuration_notes"]["runtime_route_requires_rebind"] is True
    with session_factory.begin() as session:
        rules = session.get(WorkerPoolRevision, (route["pool_id"], route["pool_revision"]))
        snapshot = worker_pool.snapshot_route(
            session, worker_pool.pool_route(rules), "python", "adapter"
        )
        fixture = Execution(
            adapter_id=adapter["id"],
            version_id=version["id"],
            trigger="manual",
            status="retry_wait",
            dispatch_backend="rabbitmq",
            dispatch_generation=1,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            route_snapshot=snapshot.model_dump(mode="json"),
            input={},
        )
        session.add(fixture)
        session.flush()
        fixture_id = fixture.id
        assert fixture.target_worker_id_snapshot is None and fixture.target_worker_id is None
        with pytest.raises(HTTPException) as rejected:
            outbox.create_dispatch_outbox(session, fixture)
        assert rejected.value.detail["code"] == "pool_routing_not_enabled"
    with session_factory() as session:
        assert attempt.retry_dispatcher_once(session) == 0
        assert session.get(Execution, fixture_id).dispatch_generation == 1
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 0
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text("UPDATE executions SET route_snapshot=NULL WHERE id=:id"), {"id": fixture_id}
            )


def test_upgrade_backfills_only_original_target_and_preserves_missing_legacy_target(
    upgrade_database,
):
    cfg = migration_config(upgrade_database)
    command.upgrade(cfg, W1_SCHEMA)
    with Session(upgrade_database) as session, session.begin():
        original, current = [
            session.scalar(
                text(
                    "INSERT INTO workers (name, status, capabilities) "
                    "VALUES (:name,'offline','[]') RETURNING id"
                ),
                {"name": name},
            )
            for name in ("original", "current")
        ]
        adapter_id = insert_legacy_adapter(session, "old routing", runtime_worker_id=current)
        version = AdapterVersion(adapter_id=adapter_id, seq=1, code="pass", requirements="")
        session.add(version)
        session.flush()
        executions = Table(
            "executions", MetaData(), autoload_with=session.connection(), resolve_fks=False
        )
        original_id, unknown_id = [
            session.scalar(
                insert(executions)
                .values(
                    adapter_id=adapter_id,
                    version_id=version.id,
                    target_worker_id=current,
                    worker_id=current,
                    target_worker_id_snapshot=frozen,
                    trigger="manual",
                    status="dead_letter",
                    input={"keep": True},
                    locale="en",
                    dependency_check=True,
                    input_source_type="json",
                    input_config_revision=1,
                    input_snapshot={"source_type": "json", "revision": 1},
                )
                .returning(executions.c.id)
            )
            for frozen in (original, None)
        ]
    command.upgrade(cfg, "head")
    with Session(upgrade_database) as session:
        kept = session.get(Execution, original_id)
        assert kept.worker_id == kept.target_worker_id == current
        assert kept.route_snapshot["route"]["worker_id"] == original
        assert kept.route_snapshot["environment"] == "unknown" and kept.input == {"keep": True}
        assert session.get(Execution, unknown_id).route_snapshot is None
        missing = attempt.execution_detail(session, unknown_id)
        assert not missing.replay_available and missing.replay_reason == "worker_unavailable"
        unclassified = session.get(Execution, unknown_id)
        unclassified.status = "retry_wait"
        unclassified.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        generation = unclassified.dispatch_generation
        deadline = unclassified.next_attempt_at
        with pytest.raises(HTTPException) as rejected:
            outbox.create_dispatch_outbox(session, unclassified)
        assert rejected.value.detail["code"] == "execution_route_unavailable"
        assert attempt.retry_dispatcher_once(session) == 0
        session.refresh(unclassified)
        assert (
            unclassified.status == "retry_wait" and unclassified.dispatch_generation == generation
        )
        assert unclassified.next_attempt_at == deadline
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 0


@pytest.mark.parametrize("fact", ["route_version", "execution_snapshot"])
def test_downgrade_refuses_new_fixed_route_facts_with_no_pool(upgrade_database, fact):
    cfg = migration_config(upgrade_database)
    command.upgrade(cfg, "head")
    with Session(upgrade_database) as session, session.begin():
        worker = Worker(name="fixed-only", status="offline", capabilities=["python"])
        session.add(worker)
        session.flush()
        adapter = Adapter(
            name="fixed-only", language="python", adapter_type="task", runtime_worker_id=worker.id
        )
        session.add(adapter)
        session.flush()
        if fact == "route_version":
            adapter.route_version = 2
        else:
            revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
            session.add(revision)
            session.flush()
            route = worker_pool.fixed_route(worker.id)
            snapshot = worker_pool.snapshot_route(session, route, "python", "current-config")
            session.add(
                Execution(
                    adapter_id=adapter.id,
                    version_id=revision.id,
                    trigger="manual",
                    status="dead_letter",
                    target_worker_id_snapshot=worker.id,
                    route_snapshot=snapshot.model_dump(mode="json"),
                    input={},
                )
            )
    with pytest.raises(DBAPIError, match="Pool routing downgrade would lose durable state"):
        command.downgrade(cfg, W1_SCHEMA)
    with Session(upgrade_database) as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
        assert session.scalar(select(func.count()).select_from(WorkerPool)) == 0
        assert session.scalar(select(Adapter.route_version)) == (
            2 if fact == "route_version" else 1
        )
        if fact == "execution_snapshot":
            assert session.scalar(select(Execution.route_snapshot))["source"] == "current-config"
