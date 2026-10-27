"""Permanent Adapter deletion keeps batch custody guards and transaction order."""

from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    SubscriptionExecutionBatch,
    SubscriptionExecutionBatchMember,
    SubscriptionLease,
    SubscriptionQuota,
)
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import adapter as adapters
from dlr.control.services import reliable_execution
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.input_config import database_now
from test_issue222_business_batch import batch_world as core_batch_world
from test_issue222_business_batch import facts, finish, produce
from test_issue222_business_batch import world as foundation_world

world = foundation_world
batch_world = core_batch_world
QUOTA_FIELDS = (
    "pending_count",
    "pending_bytes",
    "history_count",
    "history_bytes",
    "dedupe_count",
    "reserved_control_count",
    "reserved_control_bytes",
)


def retire_source(session, scope, *, unfinished=False):
    sub = session.get(EventSubscription, scope["sub"])
    sub.desired_state = "disabled"
    sub.connection_state = "stopped"
    lease = session.get(SubscriptionLease, sub.id)
    lease.owner_id = ""
    lease.closing_generation = None
    lease.lease_until = database_now(session)
    if unfinished:
        # Simulate a retained, already-deleted source with unfinished custody.
        # Adapter deletion must still enforce the purge's independent guard.
        sub.deleted_at = database_now(session)
    else:
        management.delete_subscription(session, sub.id, sub.config_version, SUPERADMIN_PRINCIPAL)


@pytest.fixture
def retired_batch(batch_world, session_factory):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        group = finish(session, execution)
        retire_source(session, batch_world)
        assert all(session.get(EventReceipt, identity).terminal_at is not None for identity in ids)
        assert facts(session, batch_world).history_bytes > 0
        return batch_world | {
            "receipts": ids,
            "execution": execution.id,
            "input": deepcopy(execution.input),
            "manifest": deepcopy(group.manifest),
            "result": deepcopy(group.result),
        }


def quota_values(session, scope):
    return {
        name: tuple(getattr(session.get(SubscriptionQuota, name), key) for key in QUOTA_FIELDS)
        for name in ("global", f"sub:{scope['sub']}")
    }


def assert_retained(session, scope, quotas):
    assert session.get(Adapter, scope["adapter"]).archived_at is None
    assert session.get(EventSubscription, scope["sub"]).deleted_at is not None
    assert all(session.get(EventReceipt, identity) is not None for identity in scope["receipts"])
    assert session.scalar(select(func.count()).select_from(EventDelivery)) == 3
    assert session.scalar(select(func.count()).select_from(SubscriptionExecutionBatchMember)) == 3
    execution = session.get(Execution, scope["execution"])
    group = session.get(SubscriptionExecutionBatch, execution.id)
    assert execution.input == scope["input"]
    assert group.manifest == scope["manifest"] and group.result == scope["result"]
    assert quota_values(session, scope) == quotas
    facts(session, scope)


def test_delete_adapter_purges_recent_terminal_batch_and_returns_all_quota(
    retired_batch, api_client, session_factory
):
    response = api_client.delete(f"/api/adapters/{retired_batch['adapter']}")
    assert response.status_code == 204, response.text
    with session_factory() as session:
        assert session.get(Adapter, retired_batch["adapter"]) is None
        assert session.get(EventSubscription, retired_batch["sub"]) is None
        assert session.get(SubscriptionQuota, f"sub:{retired_batch['sub']}") is None
        for model in (
            EventReceipt,
            EventDelivery,
            SubscriptionExecutionBatch,
            SubscriptionExecutionBatchMember,
            Execution,
            ExecutionAttempt,
            ExecutionOutbox,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        quota = session.get(SubscriptionQuota, "global")
        assert all(getattr(quota, key) == 0 for key in QUOTA_FIELDS)
        assert delivery.audit_global_quota(session)["differences"] == {}
        assert session.get(AdapterExecutionAdmission, retired_batch["adapter"]) is None
        admission = session.get(GlobalExecutionAdmission, "global")
        assert (admission.outstanding_count, admission.outstanding_bytes) == (0, 0)


@pytest.mark.parametrize("stop", [False, True])
def test_delete_adapter_never_purges_unfinished_batch(batch_world, session_factory, stop):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        group = session.get(SubscriptionExecutionBatch, execution.id)
        retire_source(session, batch_world, unfinished=True)
        scope = batch_world | {
            "receipts": ids,
            "execution": execution.id,
            "input": deepcopy(execution.input),
            "manifest": deepcopy(group.manifest),
            "result": None,
        }
        before = quota_values(session, scope)
    with session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            adapters.delete_adapter(session, scope["adapter"], stop=stop)
        assert caught.value.status_code == 409
        assert caught.value.detail["code"] == "subscription_draining"
        # Even a caller committing the handled business rejection cannot
        # persist the temporary archived flag or delete accepted custody.
        session.commit()
    with session_factory() as session:
        assert_retained(session, scope, before)
        assert session.get(Execution, scope["execution"]).status == "queued"


def test_stop_wait_commit_restores_archive_before_returning(retired_batch, session_factory):
    with session_factory.begin() as session:
        adapter = session.get(Adapter, retired_batch["adapter"])
        running = reliable_execution.accept_execution(
            session,
            adapter,
            trigger="manual",
            runtime_input=None,
            input_source_type="none",
            input_config_revision=1,
            input_snapshot={"source_type": "none", "revision": 1},
        )
        running.status = "running"
        running.worker_id = adapter.runtime_worker_id
        running.started_at = database_now(session)
        identity = running.id
        count = session.get(GlobalExecutionAdmission, "global").outstanding_count
    with session_factory() as session:
        result = adapters.delete_adapter(session, retired_batch["adapter"], stop=True)
        assert result.waiting_for_worker and result.active_execution_id == identity
    # Read in a separate transaction after the service's actual waiting commit.
    with session_factory() as session:
        assert session.get(Adapter, retired_batch["adapter"]).archived_at is None
        running = session.get(Execution, identity)
        assert running.status == "running" and running.cancel_requested
        assert session.get(GlobalExecutionAdmission, "global").outstanding_count == count
        assert session.get(SubscriptionExecutionBatch, retired_batch["execution"]) is None


@pytest.mark.parametrize("failure", ["business", "sql_statement", "sql_flush"])
def test_purge_failure_preserves_original_error_and_rolls_back_custody(
    retired_batch, session_factory, monkeypatch, failure
):
    with session_factory() as session:
        before = quota_values(session, retired_batch)
    original = delivery.purge_deleted_adapter_history
    errors = []

    def fail_after_real_purge(session, adapter_id):
        assert session.scalar(select(Adapter.archived_at).where(Adapter.id == adapter_id))
        original(session, adapter_id)
        assert session.get(SubscriptionExecutionBatch, retired_batch["execution"]) is None
        if failure == "business":
            error = adapters.domain_error(409, "delete_test_blocked", "Injected purge failure")
            errors.append(error)
            raise error
        try:
            if failure == "sql_statement":
                session.execute(text("SELECT 1 / 0"))
            else:
                session.add(SubscriptionQuota(scope="global"))
                session.flush()
        except DBAPIError as error:
            errors.append(error)
            raise

    monkeypatch.setattr(delivery, "purge_deleted_adapter_history", fail_after_real_purge)
    with session_factory() as session:
        expected = HTTPException if failure == "business" else DBAPIError
        with pytest.raises(expected) as caught:
            adapters.delete_adapter(session, retired_batch["adapter"])
        assert caught.value is errors[0]
        if failure == "business":
            assert caught.value.detail["code"] == "delete_test_blocked"
            assert (
                session.scalar(
                    select(Adapter.archived_at).where(Adapter.id == retired_batch["adapter"])
                )
                is None
            )
        else:
            assert caught.value.orig.sqlstate == (
                "22012" if failure == "sql_statement" else "23505"
            )
            if failure == "sql_flush":
                assert isinstance(caught.value, IntegrityError) and not session.is_active
        session.rollback()
    with session_factory() as session:
        assert_retained(session, retired_batch, before)
