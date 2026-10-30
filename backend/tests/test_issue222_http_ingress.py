"""HTTP custody, old-route isolation and commit ambiguity on actual PostgreSQL."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.api import ingress
from dlr.control.models import (
    Credential,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    HttpIngressRoute,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionQuota,
)
from dlr.control.services import http_ingress as service
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.input_config import database_now
from dlr.control.services.secrets import encrypt_fields
from dlr.control.services.subscription_source import ApplicationBufferBudget
from test_adapters import create_adapter
from test_credentials import create_credential
from test_issue219_delivery import world as foundation_world

world = foundation_world


@pytest.fixture
def http_world(world, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "http_ingress_enabled", True)
    with session_factory.begin() as session:
        credential = Credential(
            name="http-source-token",
            type="token",
            ciphertext=encrypt_fields({"token": "source-token"}),
        )
        session.add(credential)
        session.flush()
        sub = session.get(EventSubscription, world["sub"])
        sub.source_type = "http"
        sub.connection_state = "listening"
        config = session.get(SubscriptionConfigVersion, world["config"])
        config.source_credential_id = credential.id
        config.config = {
            "source_type": "http",
            "credential_id": credential.id,
            "mode": "per_event",
            "ordering": "receipt_serial",
            "limits": {},
        }
        route = HttpIngressRoute(
            public_id=uuid4().hex, subscription_id=sub.id, source_epoch=sub.source_epoch
        )
        session.add(route)
        result = {**world, "public_id": route.public_id, "credential": credential.id}
    pool = ingress.IngressResources(
        buffer=ApplicationBufferBudget(), session_factory=session_factory
    )
    monkeypatch.setattr(ingress, "resources", lambda: pool)
    yield result
    pool.close()


def post(client, world, key="one", body=b'{"value": 1}', token="source-token"):
    return client.post(
        f"/api/ingress/{world['public_id']}",
        content=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": key,
        },
    )


def test_first_receipt_duplicate_raw_conflict_and_safe_status(
    api_client, http_world, session_factory
):
    first = post(api_client, http_world)
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["execution_id"] is None and result["processing_status"] == "pending"
    duplicate = post(api_client, http_world)
    assert duplicate.status_code == 200 and duplicate.json()["receipt_id"] == result["receipt_id"]
    conflict = post(api_client, http_world, body=b'{"value":1}')
    assert (
        conflict.status_code == 409
        and conflict.json()["detail"]["code"] == "event_identity_conflict"
    )
    assert post(api_client, http_world, key="two").status_code == 202
    observed = api_client.get(
        result["status_url"], headers={"Authorization": "Bearer source-token"}
    )
    assert observed.status_code == 200
    assert not {"envelope", "payload", "source_raw_body", "code", "output"} & set(observed.json())
    with session_factory() as session:
        receipt = session.get(EventReceipt, result["receipt_id"])
        assert receipt.content_hash == hashlib.sha256(b'{"value": 1}').hexdigest()
        assert receipt.source_raw_body == b'{"value": 1}'
        assert receipt.envelope["data"]["payload"] == {"value": 1}
        assert receipt.payload_bytes > len(receipt.source_raw_body)
        checkpoint = session.scalar(
            select(SourceCheckpoint).where(SourceCheckpoint.subscription_id == http_world["sub"])
        )
        assert checkpoint.blocked_unaccepted is None
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        quota = session.get(SubscriptionQuota, f"sub:{http_world['sub']}")
        assert quota.pending_count == quota.history_count == quota.dedupe_count == 2
        assert delivery.audit_quota(session, http_world["sub"])["differences"] == {}


def test_disabled_feature_full_quota_and_expired_owner_preserve_readback(
    api_client, http_world, session_factory, monkeypatch
):
    first = post(api_client, http_world)
    assert first.status_code == 202
    with session_factory.begin() as session:
        quota = session.get(SubscriptionQuota, f"sub:{http_world['sub']}")
        quota.blocked_reason = "subscription_capacity_exceeded"
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    assert post(api_client, http_world).status_code == 200
    rejected = post(api_client, http_world, key="new")
    assert rejected.status_code == 503 and rejected.headers["Retry-After"] == "1"
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    assert post(api_client, http_world, key="new").status_code == 202
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, http_world["sub"])
        management._fence(session, sub)
        sub.desired_state = "disabled"
    assert post(api_client, http_world).status_code == 200
    rejected = post(api_client, http_world, key="newer")
    assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "ingress_disabled"
    assert (
        api_client.get(
            first.json()["status_url"], headers={"Authorization": "Bearer source-token"}
        ).status_code
        == 200
    )


def test_current_credential_authentication_and_cross_source_query(
    api_client, http_world, session_factory
):
    accepted = post(api_client, http_world)
    assert accepted.status_code == 202
    for token in ["wrong", "test-admin-token"]:
        assert post(api_client, http_world, token=token).status_code == 401
        assert (
            api_client.get(
                accepted.json()["status_url"], headers={"Authorization": "Bearer " + token}
            ).status_code
            == 401
        )
    second_adapter = create_adapter(api_client, name="second-http-source")
    with session_factory.begin() as session:
        credential = session.get(Credential, http_world["credential"])
        credential.ciphertext = encrypt_fields({"token": "rotated-token"})
        other = HttpIngressRoute(
            public_id=uuid4().hex, subscription_id=http_world["sub"], source_epoch="different-epoch"
        )
        session.add(other)
        other_path = f"/api/ingress/{other.public_id}/receipts/{accepted.json()['receipt_id']}"
        first_subscription = session.get(EventSubscription, http_world["sub"])
        second_credential = Credential(
            name="second-http-source-token",
            type="token",
            ciphertext=encrypt_fields({"token": "second-source-token"}),
        )
        second_subscription = EventSubscription(
            adapter_id=second_adapter["id"],
            source_type="http",
            name="second-http-source",
            # Epochs are scoped to a subscription. Keep it equal so the epoch
            # filter cannot mask a missing subscription-isolation predicate.
            source_epoch=first_subscription.source_epoch,
        )
        session.add_all([second_credential, second_subscription])
        session.flush()
        assert second_subscription.id != first_subscription.id
        second_config = SubscriptionConfigVersion(
            subscription_id=second_subscription.id,
            version=1,
            source_epoch=second_subscription.source_epoch,
            source_credential_id=second_credential.id,
            config={
                "source_type": "http",
                "credential_id": second_credential.id,
                "mode": "per_event",
                "ordering": "receipt_serial",
                "limits": {},
            },
        )
        second_route = HttpIngressRoute(
            public_id=uuid4().hex,
            subscription_id=second_subscription.id,
            source_epoch=second_subscription.source_epoch,
        )
        session.add_all([second_config, second_route])
        second_path = (
            f"/api/ingress/{second_route.public_id}/receipts/{accepted.json()['receipt_id']}"
        )
    assert post(api_client, http_world).status_code == 401
    assert post(api_client, http_world, token="rotated-token").status_code == 200
    assert (
        api_client.get(other_path, headers={"Authorization": "Bearer rotated-token"}).status_code
        == 404
    )
    own_status = api_client.get(
        accepted.json()["status_url"], headers={"Authorization": "Bearer rotated-token"}
    )
    assert own_status.status_code == 200
    assert own_status.json()["receipt_id"] == accepted.json()["receipt_id"]
    cross_source = api_client.get(
        second_path, headers={"Authorization": "Bearer second-source-token"}
    )
    assert cross_source.status_code == 404
    assert cross_source.json()["detail"]["code"] == "ingress_receipt_not_found"
    assert (
        api_client.get(
            accepted.json()["status_url"],
            headers={"Authorization": "Bearer second-source-token"},
        ).status_code
        == 401
    )
    # External credentials cannot obtain Adapter administrative access.
    assert (
        api_client.post(
            f"/api/event-subscriptions/{http_world['sub']}/disable",
            json={"expected_config_version": 1},
            headers={"Authorization": "Bearer rotated-token"},
        ).status_code
        == 401
    )


def test_old_route_never_accepts_new_epoch_and_deleted_route_stays_closed(
    api_client, http_world, session_factory
):
    accepted = post(api_client, http_world)
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, http_world["sub"])
        sub.source_epoch = "epoch-two"
        sub.config_version = 2
        value = {"source_type": "http", "credential_id": http_world["credential"], "limits": {}}
        management._new_config(session, sub, value)
        sub.activation_id = None
    assert post(api_client, http_world).status_code == 200
    assert post(api_client, http_world, key="never-seen").status_code == 410
    assert (
        api_client.get(
            accepted.json()["status_url"], headers={"Authorization": "Bearer source-token"}
        ).status_code
        == 200
    )
    with session_factory.begin() as session:
        session.get(EventSubscription, http_world["sub"]).deleted_at = database_now(session)
    assert post(api_client, http_world).status_code == 404
    assert (
        api_client.get(
            accepted.json()["status_url"], headers={"Authorization": "Bearer source-token"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_failure_never_claims_202_and_retry_uses_same_identity(
    api_client, http_world, session_factory, monkeypatch, after_commit
):
    class BrokenCommit(Session):
        def commit(self) -> None:
            if after_commit:
                super().commit()
            raise OperationalError(None, None, RuntimeError("simulated connection loss"))

    pool = ingress.resources()
    monkeypatch.setattr(
        pool,
        "session_factory",
        sessionmaker(bind=session_factory.kw["bind"], class_=BrokenCommit, expire_on_commit=False),
    )
    response = post(api_client, http_world)
    assert response.status_code == 503
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == int(after_commit)
    monkeypatch.setattr(pool, "session_factory", session_factory)
    retried = post(api_client, http_world)
    assert retried.status_code == (200 if after_commit else 202)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 1
        assert delivery.audit_quota(session, http_world["sub"])["differences"] == {}


def test_precommit_rollback_keeps_no_partial_responsibility(http_world, session_factory):
    with pytest.raises(RuntimeError), session_factory.begin() as session:
        status, _ = service.receive(
            session, http_world["public_id"], "Bearer source-token", "one", b"[]"
        )
        assert status == 202
        raise RuntimeError("injected before custody commit")
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == 0
        assert session.get(SubscriptionQuota, f"sub:{http_world['sub']}") is None


def test_unfinished_raw_input_is_retained_then_terminal_expiry_releases_bytes(
    api_client, http_world, session_factory
):
    response = post(api_client, http_world)
    rid = response.json()["receipt_id"]
    with session_factory.begin() as session:
        receipt = session.get(EventReceipt, rid)
        receipt.first_received_at = database_now(session) - timedelta(days=40)
        assert delivery.cleanup_history(session, http_world["sub"])["payloads_expired"] == 0
        assert receipt.source_raw_body is not None
    with session_factory.begin() as session:
        delivery.skip_event(session, rid, "expiry-skip", None, "test expiry")
        receipt = session.get(EventReceipt, rid)
        receipt.terminal_at = database_now(session) - timedelta(days=8)
        result = delivery.cleanup_history(session, http_world["sub"])
        assert result["payloads_expired"] == 1
        assert receipt.source_raw_body is None and receipt.envelope is None
        assert delivery.audit_quota(session, http_world["sub"])["differences"] == {}


def test_concurrent_same_identity_commits_one_responsibility(http_world, session_factory):
    barrier = threading.Barrier(4)

    def accept() -> tuple[int, int]:
        barrier.wait(5)
        with session_factory.begin() as session:
            status, response = service.receive(
                session, http_world["public_id"], "Bearer source-token", "concurrent", b"42"
            )
            return status, response["receipt_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: accept(), range(4)))
    assert sorted(status for status, _ in results) == [200, 200, 200, 202]
    assert len({identity for _, identity in results}) == 1
    with session_factory() as session:
        assert delivery.audit_quota(session, http_world["sub"])["differences"] == {}


def test_http_source_bearer_also_works_on_account_entry_without_csrf(api_client, http_world):
    path = f"/__dlr_account/api/ingress/{http_world['public_id']}"
    response = api_client.post(
        path,
        content=b"{}",
        headers={
            "Authorization": "Bearer source-token",
            "Idempotency-Key": "account-entry",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202, response.text
    observed = api_client.get(
        "/__dlr_account" + response.json()["status_url"],
        headers={"Authorization": "Bearer source-token"},
    )
    assert observed.status_code == 200


def test_dedicated_ingress_pool_has_real_connection_and_lock_deadlines(
    session_factory, monkeypatch
):
    monkeypatch.setattr(
        settings,
        "database_url",
        session_factory.kw["bind"].url.render_as_string(hide_password=False),
    )
    pool = ingress.IngressResources(buffer=ApplicationBufferBudget())
    try:
        with pool.session_factory() as session:
            assert session.scalar(text("SHOW statement_timeout")) == "5s"
            assert session.scalar(text("SHOW lock_timeout")) == "1s"
            assert session.scalar(text("SELECT 1")) == 1
        assert pool.engine.pool.size() == 8 and pool.engine.pool._max_overflow == 0
    finally:
        pool.close()


def test_saved_http_routes_reset_clone_and_recreation_are_distinct(
    api_client, session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    monkeypatch.setattr(settings, "http_ingress_enabled", True)
    account = create_credential(
        api_client, name="source-token", type_="token", fields={"token": "source-token"}
    )
    adapter = create_adapter(api_client)
    config = {"source_type": "http", "credential_id": account["id"]}
    created = api_client.post(
        "/api/event-subscriptions", json={"adapter_id": adapter["id"], "config": config}
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["desired_state"] == "disabled" and sub["ingress_path"]
    first_path = sub["ingress_path"]
    reset = api_client.post(
        f"/api/event-subscriptions/{sub['id']}/reset",
        json={
            "expected_config_version": 1,
            "request_id": "reset-one",
            "reason": "new source identity",
        },
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["ingress_path"] != first_path
    pool = ingress.IngressResources(
        buffer=ApplicationBufferBudget(), session_factory=session_factory
    )
    monkeypatch.setattr(ingress, "resources", lambda: pool)
    try:
        old = api_client.post(
            first_path,
            content=b"{}",
            headers={
                "Authorization": "Bearer source-token",
                "Idempotency-Key": "late",
                "Content-Type": "application/json",
            },
        )
        assert old.status_code == 410
        target = create_adapter(api_client, name="copy-http")
        with session_factory.begin() as session:
            clone = management.clone_for_adapter(session, adapter["id"], target["id"])
            assert clone is not None
            clone_path = service.ingress_path(session, clone)
            assert clone_path not in {first_path, reset.json()["ingress_path"]}
        deleted = api_client.request(
            "DELETE", f"/api/event-subscriptions/{sub['id']}", json={"expected_config_version": 2}
        )
        assert deleted.status_code == 204, deleted.text
        recreated = api_client.post(
            "/api/event-subscriptions", json={"adapter_id": adapter["id"], "config": config}
        )
        assert recreated.status_code == 201
        assert recreated.json()["ingress_path"] not in {
            first_path,
            reset.json()["ingress_path"],
            clone_path,
        }
        assert (
            api_client.post(
                first_path,
                content=b"{}",
                headers={
                    "Authorization": "Bearer source-token",
                    "Idempotency-Key": "late",
                    "Content-Type": "application/json",
                },
            ).status_code
            == 404
        )
    finally:
        pool.close()


def test_numeric_body_remains_canonical_after_actual_jsonb_roundtrip(
    api_client, http_world, session_factory
):
    # PostgreSQL may render an exponent as an integer. Do not accept a float
    # which becomes an out-of-domain integer before T2 canonicalization.
    from dlr.control.services.event_envelope import validate_envelope

    for index, raw in enumerate([b'{"value":9007199254740991.0}', b'{"value":1e-308}']):
        response = post(api_client, http_world, key=f"safe-number-{index}", body=raw)
        assert response.status_code == 202, response.text
        with session_factory() as session:
            receipt = session.get(EventReceipt, response.json()["receipt_id"])
            validate_envelope(receipt.envelope)
    for index, raw in enumerate([b'{"value":1e20}', b'{"value":-1e20}', b'{"value":1e308}']):
        response = post(api_client, http_world, key=f"unsafe-number-{index}", body=raw)
        assert response.status_code == 422
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 2
