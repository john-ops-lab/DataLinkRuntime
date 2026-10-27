"""Real PostgreSQL custody, immutable membership, strict finish and shared TTL."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from dlr.common.config import settings
from dlr.control.models import (
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionExecutionBatch,
    SubscriptionExecutionBatchMember,
    SubscriptionQuota,
)
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services import subscription_business_batch as batch
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.ingress_capacity import DEFAULT_GLOBAL_LIMITS, DEFAULT_SUBSCRIPTION_LIMITS
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_batch_protocol import RESULT_SCHEMA, digest, encode
from test_issue219_delivery import receive as single_receive
from test_issue219_delivery import world as foundation_world
from test_issue219_queue_contract import queue_config, queue_envelope

world = foundation_world


@pytest.fixture
def batch_world(world, session_factory, monkeypatch):
    monkeypatch.setattr(
        settings,
        "ingress_capacity_profile",
        json.dumps(
            {
                "global": DEFAULT_GLOBAL_LIMITS | {"rate": 1000},
                "subscription_max": DEFAULT_SUBSCRIPTION_LIMITS | {"rate": 1000},
            }
        ),
    )
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, world["sub"])
        sub.source_type = "rabbitmq"
        sub.connection_state = "listening"
        config = session.get(SubscriptionConfigVersion, world["config"])
        config.config = queue_config() | {"mode": "business_batch", "limits": {"rate": 1000}}
        revision = session.get(AdapterVersion, world["revision"])
        revision.runtime_config = {"event_batch_schema": "dlr.event.batch.v1"}
    return world


def receive(session, world, index=0, body=b"test"):
    return delivery.receive_event(
        session,
        world["sub"],
        "owner-a",
        world["generation"],
        queue_envelope(world, message_id=f"message-{index}", body=body),
        "queue",
        {},
    )[0]


def produce(session, world, count=3, body=b"test"):
    receipts = [receive(session, world, index, body) for index in range(count)]
    ids = [receipt.id for receipt in receipts]
    head = session.scalar(select(EventDelivery.id).where(EventDelivery.receipt_id == ids[0]))
    execution = batch.materialize_batch(session, head)
    assert execution is not None
    return ids, execution


def facts(session, world):
    assert delivery.audit_quota(session, world["sub"])["differences"] == {}
    assert delivery.audit_global_quota(session)["differences"] == {}
    return session.get(SubscriptionQuota, f"sub:{world['sub']}")


def finish(session, execution, statuses=None):
    group = session.get(SubscriptionExecutionBatch, execution.id)
    members = [
        {
            key: item[key]
            for key in ("receipt_id", "delivery_id", "delivery_ordinal", "member_index")
        }
        | {"status": status, "error_code": code}
        for item, (status, code) in zip(
            group.manifest, statuses or [("succeeded", None)] * group.member_count, strict=True
        )
    ]
    value = {
        "schema": RESULT_SCHEMA,
        "batch_id": group.batch_id,
        "manifest_hash": group.manifest_hash,
        "members": members,
    }
    decision = batch.validate_finish(session, execution, value, value, False, False, "succeeded")
    activation = session.get(SubscriptionActivation, group.activation_id)
    attempt = ExecutionAttempt(
        execution_id=execution.id,
        adapter_id=execution.adapter_id,
        attempt_no=1,
        worker_id=activation.worker_id,
        fencing_token=1,
        status="succeeded",
        claimed_at=database_now(session),
        lease_expires_at=database_now(session) + timedelta(seconds=60),
    )
    session.add(attempt)
    session.flush()
    batch.persist_finish(session, execution, attempt.id, decision)
    execution.status = decision.execution_status
    execution.output = decision.output
    assert batch.settle_batch(session, execution.id) == group.member_count
    return group


def test_t1_prepays_complete_frame_and_t2_atomic_membership(batch_world, session_factory):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        group = session.get(SubscriptionExecutionBatch, execution.id)
        assert group.member_count == 3 and group.manifest_hash == digest(group.manifest)
        assert group.input_bytes == len(encode(execution.input)) <= 256 * 1024
        assert execution.subscription_delivery_kind == "batch"
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 1
        assert (
            session.scalar(select(func.count()).select_from(SubscriptionExecutionBatchMember)) == 3
        )
        quota = facts(session, batch_world)
        assert quota.pending_count == 0 and quota.history_count == 3
        for receipt_id in ids:
            receipt = session.get(EventReceipt, receipt_id)
            item = session.scalar(
                select(EventDelivery).where(EventDelivery.receipt_id == receipt_id)
            )
            assert receipt.state == item.state == "processing"
            assert item.execution_id is None and item.batch_execution_id == execution.id
            assert (
                item.retained_execution_bytes
                == accounting.batch_budget(receipt.envelope).input_bytes
            )
            assert item.retained_batch_bytes == 8192


def test_t2_rollback_keeps_all_custody_without_partial_outbox(
    batch_world, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        for i in range(3):
            receive(session, batch_world, i)
        head = session.scalar(select(EventDelivery.id).order_by(EventDelivery.id))
    from dlr.control.services import reliable_execution

    original = reliable_execution.accept_execution

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after shared execution/outbox")

    monkeypatch.setattr(reliable_execution, "accept_execution", fail_after_insert)
    with pytest.raises(RuntimeError), session_factory.begin() as session:
        batch.materialize_batch(session, head)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert (
            session.scalar(select(func.count()).select_from(SubscriptionExecutionBatchMember)) == 0
        )
        assert facts(session, batch_world).pending_count == 3


def test_contiguous_prefix_stops_at_future_retry_and_binding_boundary(batch_world, session_factory):
    with session_factory.begin() as session:
        receipts = [receive(session, batch_world, i) for i in range(3)]
        rows = session.scalars(select(EventDelivery).order_by(EventDelivery.id)).all()
        rows[1].retry_after = database_now(session) + timedelta(seconds=60)
        execution = batch.materialize_batch(session, rows[0].id)
        assert session.get(SubscriptionExecutionBatch, execution.id).member_count == 1
        assert receipts[1].state == receipts[2].state == "pending"
        assert facts(session, batch_world).pending_count == 2


def test_same_configuration_but_distinct_activation_stops_prefix(batch_world, session_factory):
    with session_factory.begin() as session:
        first = receive(session, batch_world)
        original = session.get(SubscriptionActivation, first.activation_id)
        second = SubscriptionActivation(
            subscription_id=original.subscription_id,
            config_version_id=original.config_version_id,
            revision_id=original.revision_id,
            worker_id=original.worker_id,
            execution_policy_snapshot=deepcopy(original.execution_policy_snapshot),
        )
        session.add(second)
        session.flush()
        session.get(EventSubscription, batch_world["sub"]).activation_id = second.id
        following = receive(session, batch_world, 1)
        head = session.scalar(select(EventDelivery.id).where(EventDelivery.receipt_id == first.id))
        execution = delivery.dispatch_delivery(session, head)
        assert session.get(SubscriptionExecutionBatch, execution.id).member_count == 1
        assert following.state == "pending"
        assert facts(session, batch_world).pending_count == 1


def test_batch_count_and_complete_input_bytes_are_bounded(batch_world, session_factory):
    with session_factory.begin() as session:
        _, execution = produce(session, batch_world, 101)
        assert session.get(SubscriptionExecutionBatch, execution.id).member_count == 100
        assert facts(session, batch_world).pending_count == 1


def test_frame_byte_limit_keeps_suffix_pending(batch_world, session_factory):
    with session_factory.begin() as session:
        _, execution = produce(session, batch_world, 4, b"x" * 60000)
        group = session.get(SubscriptionExecutionBatch, execution.id)
        assert 1 <= group.member_count < 4 and group.input_bytes <= 256 * 1024
        assert facts(session, batch_world).pending_count == 4 - group.member_count


def test_success_partial_failure_and_retry_wait_have_distinct_facts(batch_world, session_factory):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        assert (
            batch.validate_finish(session, execution, None, None, False, False, "retry_wait")
            is None
        )
        assert session.get(SubscriptionExecutionBatch, execution.id).result is None
        group = finish(
            session,
            execution,
            [("succeeded", None), ("failed", "business_error"), ("not_run", "predecessor_failed")],
        )
        assert group.result_counts == {"succeeded": 1, "failed": 1, "unknown": 0, "not_run": 1}
        assert [session.get(EventReceipt, i).state for i in ids] == [
            "succeeded",
            "failed",
            "failed",
        ]
        checkpoint = session.scalar(
            select(SourceCheckpoint).where(SourceCheckpoint.subscription_id == batch_world["sub"])
        )
        assert checkpoint.blocked_receipt_id == ids[1]
        assert batch.settle_batch(session, execution.id) == 0
        assert facts(session, batch_world).history_count == 3


@pytest.mark.parametrize(
    ("mutation", "guard_message"),
    [
        pytest.param(
            "remove_member", "batch member set differs from frozen manifest", id="remove_member"
        ),
        pytest.param(
            "rewrite_input",
            "shared batch input is immutable until whole group cleanup",
            id="rewrite_input",
        ),
        pytest.param("rewrite_kind", "execution delivery kind is immutable", id="rewrite_kind"),
        pytest.param(
            "orphan_delivery", "batch member set differs from frozen manifest", id="orphan_delivery"
        ),
        pytest.param(
            "receipt_identity", "batch receipt identity is immutable", id="receipt_identity"
        ),
        pytest.param(
            "receipt_envelope",
            "batch receipt payload is immutable before TTL",
            id="receipt_envelope",
        ),
    ],
)
def test_database_rejects_mutation_of_frozen_membership(
    batch_world, session_factory, mutation, guard_message
):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        execution_id = execution.id
    with pytest.raises(IntegrityError) as rejected, session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        if mutation == "remove_member":
            session.execute(
                delete(SubscriptionExecutionBatchMember).where(
                    SubscriptionExecutionBatchMember.execution_id == execution_id,
                    SubscriptionExecutionBatchMember.member_index == 2,
                )
            )
        elif mutation == "rewrite_input":
            execution.input = {**execution.input, "manifest_hash": "x" * 64}
        elif mutation == "rewrite_kind":
            execution.subscription_delivery_kind = "single"
        elif mutation == "receipt_identity":
            session.get(EventReceipt, ids[0]).shard = "another-shard"
        elif mutation == "receipt_envelope":
            session.get(EventReceipt, ids[0]).envelope = {"replacement": "not original"}
        else:
            receipt = receive(session, batch_world, 99)
            item = session.scalar(
                select(EventDelivery).where(EventDelivery.receipt_id == receipt.id)
            )
            item.batch_execution_id = execution_id
    assert rejected.value.orig.sqlstate == "23514"
    assert rejected.value.orig.diag.message_primary == guard_message


def test_terminal_requires_strict_result_and_missing_result_becomes_unknown(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        _, execution = produce(session, batch_world)
        execution_id = execution.id
    with pytest.raises(IntegrityError) as rejected, session_factory.begin() as session:
        session.get(Execution, execution_id).status = "succeeded"
    assert rejected.value.orig.sqlstate == "23514"
    assert (
        rejected.value.orig.diag.message_primary == "batch terminal lacks validated member result"
    )
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        execution.status = "cancelled"
        execution.output_size = 9999999
        execution.output_truncated = True
        execution.output_preview = "untrusted old report"
        batch.finalize_on_terminal(session, execution)
        assert execution.output_size == len(encode(execution.output))
        assert not execution.output_truncated and execution.output_preview is None
        assert batch.settle_batch(session, execution.id) == 3
        assert session.get(SubscriptionExecutionBatch, execution.id).result_counts["unknown"] == 3


def test_seven_day_keeps_shared_copy_and_one_recent_member_protects_whole_group(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        group = finish(session, execution)
        execution_id = execution.id
        shared_input = deepcopy(execution.input)
        retained_bytes = group.retained_input_bytes + group.retained_batch_bytes
        for identity in ids:
            session.get(EventReceipt, identity).terminal_at = database_now(session) - timedelta(
                days=31
            )
        session.get(EventReceipt, ids[-1]).terminal_at = database_now(session) - timedelta(days=8)
    with session_factory.begin() as session:
        result = delivery.cleanup_history(session, batch_world["sub"])
        assert result["payloads_expired"] == 3 and result["metadata_deleted"] == 0
        assert session.get(Execution, execution_id).input == shared_input
        assert session.get(SubscriptionExecutionBatch, execution_id) is not None
        assert facts(session, batch_world).history_bytes == retained_bytes + 3 * 4608
        session.get(EventReceipt, ids[-1]).terminal_at = database_now(session) - timedelta(days=31)
    with session_factory.begin() as session:
        assert delivery.cleanup_history(session, batch_world["sub"])["metadata_deleted"] == 3
        assert session.get(SubscriptionExecutionBatch, execution_id) is None
        assert session.get(Execution, execution_id).input is None
        assert session.get(Execution, execution_id).output is None
        assert facts(session, batch_world).history_bytes == 0


def test_one_member_replay_remains_batch_and_original_group_stays_protected(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world)
        finish(
            session,
            execution,
            [
                ("failed", "business_error"),
                ("not_run", "predecessor_failed"),
                ("not_run", "predecessor_failed"),
            ],
        )
        original_id = execution.id
        replay = delivery.replay_event(session, ids[0], "replay-one", None, "correct one member")
        rerun = delivery.dispatch_delivery(session, replay.id)
        assert rerun.id != original_id
        assert session.get(SubscriptionExecutionBatch, rerun.id).member_count == 1
        assert session.get(SubscriptionExecutionBatch, original_id).member_count == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(SubscriptionExecutionBatchMember)
                .where(SubscriptionExecutionBatchMember.receipt_id == ids[0])
            )
            == 2
        )
        assert facts(session, batch_world).history_count == 4


def test_member_replay_isolated_from_new_pending_preserves_fifo_and_normal_batch(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        ids, original = produce(session, batch_world, count=2)
        pending = [receive(session, batch_world, index) for index in (2, 3)]
        pending_deliveries = [
            session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == row.id))
            for row in pending
        ]
        assert delivery.dispatch_delivery(session, pending_deliveries[0].id) is None
        finish(session, original, [("succeeded", None), ("failed", "business_error")])
        original_batch = session.get(SubscriptionExecutionBatch, original.id)
        original_input = deepcopy(original.input)
        original_manifest = deepcopy(original_batch.manifest)
        original_result = deepcopy(original_batch.result)
        original_output = deepcopy(original.output)
        receipt = session.get(EventReceipt, ids[1])
        original_envelope = deepcopy(receipt.envelope)
        session.get(EventSubscription, batch_world["sub"]).desired_state = "disabled"

        assert delivery.dispatch_delivery(session, pending_deliveries[0].id) is None
        replay = delivery.replay_event(
            session, receipt.id, "replay-before-new-pending", None, "repair only selected member"
        )
        assert replay.ordinal == 2
        assert delivery.dispatch_delivery(session, pending_deliveries[0].id) is None
        repeated = delivery.dispatch_delivery(session, replay.id)
        assert repeated is not None and repeated.id != original.id
        replay_batch = session.get(SubscriptionExecutionBatch, repeated.id)
        assert replay_batch.member_count == 1
        assert replay_batch.batch_id != original_batch.batch_id
        assert replay_batch.manifest_hash == digest(replay_batch.manifest)
        assert replay_batch.manifest_hash != original_batch.manifest_hash
        assert replay_batch.activation_id == original_batch.activation_id
        assert replay_batch.revision_id == original_batch.revision_id
        assert replay_batch.revision_override_id == original_batch.revision_override_id
        assert repeated.version_id == original.version_id
        assert repeated.target_worker_id_snapshot == original.target_worker_id_snapshot
        assert replay_batch.manifest[0] == {
            "receipt_id": receipt.id,
            "delivery_id": replay.id,
            "delivery_ordinal": 2,
            "member_index": 0,
            "content_hash": receipt.content_hash,
            "envelope_hash": digest(original_envelope),
        }
        assert repeated.input["schema"] == "dlr.event.batch.v1"
        assert repeated.input["batch_id"] == replay_batch.batch_id
        assert repeated.input["manifest_hash"] == replay_batch.manifest_hash
        assert len(repeated.input["members"]) == 1
        assert repeated.input["members"][0]["input"] == original_envelope
        assert all(
            row.state == item.state == "pending"
            for row, item in zip(pending, pending_deliveries, strict=True)
        )
        assert all(batch.effective_execution_id(item) is None for item in pending_deliveries)
        assert delivery.dispatch_delivery(session, pending_deliveries[0].id) is None

        finish(session, repeated)
        successor = delivery.dispatch_delivery(session, pending_deliveries[0].id)
        assert successor is not None
        successor_batch = session.get(SubscriptionExecutionBatch, successor.id)
        assert successor_batch.member_count == 2
        assert [item["receipt_id"] for item in successor_batch.manifest] == [
            row.id for row in pending
        ]
        assert [item["delivery_ordinal"] for item in successor_batch.manifest] == [1, 1]
        assert [item["member_index"] for item in successor_batch.manifest] == [0, 1]
        assert original.input == original_input
        assert original.output == original_output
        assert original_batch.manifest == original_manifest
        assert original_batch.result == original_result
        assert receipt.envelope == original_envelope
        original_delivery = session.get(EventDelivery, original_manifest[1]["delivery_id"])
        assert original_delivery.state == original_delivery.batch_member_outcome == "failed"
        assert original_batch.result_counts == {
            "succeeded": 1,
            "failed": 1,
            "unknown": 0,
            "not_run": 0,
        }
        facts(session, batch_world)


def test_overlapping_replay_groups_gc_their_own_shares_without_deleting_shared_receipt(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        ids, original = produce(session, batch_world)
        finish(session, original)
        session.get(EventSubscription, batch_world["sub"]).desired_state = "disabled"
        replay = delivery.replay_event(session, ids[0], "after-success", None, "reconcile again")
        repeated = delivery.dispatch_delivery(session, replay.id)
        finish(session, repeated)
        execution_ids = [original.id, repeated.id]
        assert facts(session, batch_world).history_count == 4
        for identity in ids:
            session.get(EventReceipt, identity).terminal_at = database_now(session) - timedelta(
                days=31
            )
    with session_factory.begin() as session:
        # Three-member original group fits; the singleton replay stays, and
        # its Receipt remains authoritative until its own whole group clears.
        assert batch.cleanup_batches(session, batch_world["sub"], limit=3)["members"] == 3
        assert session.get(EventReceipt, ids[0]) is not None
        assert session.get(Execution, execution_ids[0]).input is None
        assert session.get(Execution, execution_ids[1]).input is not None
        assert facts(session, batch_world).history_count == 1
    with session_factory.begin() as session:
        assert delivery.cleanup_history(session, batch_world["sub"])["metadata_deleted"] == 3
        assert all(session.get(Execution, identity).input is None for identity in execution_ids)
        assert facts(session, batch_world).history_bytes == 0


def test_repeatable_read_owner_snapshot_cannot_mix_single_and_batch(world, session_factory):
    with session_factory.begin() as session:
        receipt = single_receive(session, world)
        item_id = session.scalar(
            select(EventDelivery.id).where(EventDelivery.receipt_id == receipt.id)
        )
        activation_id = receipt.activation_id
        unowned = Execution(
            adapter_id=world["adapter"],
            version_id=world["revision"],
            trigger="subscription",
            input={},
        )
        session.add(unowned)
        session.flush()
        execution_id = unowned.id
    engine = session_factory.kw["bind"]
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as old_reader:
        tx = old_reader.begin()
        assert (
            old_reader.scalar(
                select(Execution.subscription_delivery_kind).where(Execution.id == execution_id)
            )
            is None
        )
        with session_factory.begin() as writer:
            writer.get(EventDelivery, item_id).execution_id = execution_id
        with pytest.raises(DBAPIError) as failure:
            old_reader.execute(
                SubscriptionExecutionBatch.__table__.insert().values(
                    execution_id=execution_id,
                    batch_id="b" * 32,
                    subscription_id=world["sub"],
                    source_epoch="epoch-1",
                    shard="test:0",
                    activation_id=activation_id,
                    revision_id=world["revision"],
                    manifest=[],
                    manifest_hash="f" * 64,
                    member_count=1,
                    input_bytes=1,
                    retained_input_bytes=1,
                    retained_batch_bytes=8192,
                )
            )
        assert failure.value.orig.sqlstate == "40001"
        tx.rollback()
    with session_factory() as session:
        assert session.get(Execution, execution_id).subscription_delivery_kind == "single"
        assert session.get(SubscriptionExecutionBatch, execution_id) is None
        unique = session.scalar(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='event_deliveries'::regclass "
                "AND conname='uq_delivery_execution'"
            )
        )
        assert unique == 1


def test_old_binary_cannot_accept_batch_custody_without_prepaid_member_budget(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        receipt = receive(session, batch_world)
        receipt_id = receipt.id
    with pytest.raises(IntegrityError) as rejected, session_factory.begin() as session:
        # Old T1 omits all new fields. PostgreSQL defaults must not turn that
        # write into an ACK-authorizing but unexecutable batch responsibility.
        session.execute(
            EventDelivery.__table__.insert().values(
                receipt_id=receipt_id,
                ordinal=2,
                state="pending",
                retained_execution_bytes=100,
            )
        )
    assert rejected.value.orig.sqlstate == "23514"
    assert (
        rejected.value.orig.diag.message_primary == "business batch requires prepaid bounded shares"
    )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == 1
        assert facts(session, batch_world).pending_count == 1


def test_large_candidate_suffix_is_not_loaded_before_byte_bounded_prefix(
    batch_world, session_factory
):
    with session_factory.begin() as session:
        for index in range(100):
            receive(session, batch_world, index, b"x" * 60000)
        head = session.scalar(select(EventDelivery.id).order_by(EventDelivery.id))
    statements = []
    engine = session_factory.kw["bind"]

    def observe(connection, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with session_factory.begin() as session:
            execution = batch.materialize_batch(session, head)
            group = session.get(SubscriptionExecutionBatch, execution.id)
            assert 1 <= group.member_count <= 3
            assert group.input_bytes <= 256 * 1024
            assert facts(session, batch_world).pending_count == 100 - group.member_count
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    lightweight = [sql for sql in statements if "LIMIT" in sql and "event_receipts.sequence" in sql]
    assert lightweight and all("event_receipts.envelope" not in sql for sql in lightweight)
    body_queries = [
        sql
        for sql in statements
        if "event_receipts.envelope" in sql and "JOIN event_deliveries" in sql
    ]
    assert len(body_queries) == 1
    assert "event_deliveries.id IN" in body_queries[0]
    assert "event_receipts.source_raw_body" not in body_queries[0]
