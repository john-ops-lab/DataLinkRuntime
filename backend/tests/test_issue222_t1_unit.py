"""Bounded T1 custody uses real PostgreSQL, original identities and actual commits."""

import base64
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.services import ingress_capacity
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_t1_unit as service
from dlr.control.services.adapter import domain_error
from dlr.control.services.event_envelope import validate_envelope
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_t1_unit import T1Input, T1Unit
from test_issue219_delivery import world as foundation_world
from test_issue219_queue_contract import queue_envelope
from test_issue222_business_batch import batch_world as queue_world

world = foundation_world
batch_world = queue_world


@pytest.fixture
def http_world(batch_world, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "http_ingress_enabled", True)
    with session_factory.begin() as session:
        session.get(EventSubscription, batch_world["sub"]).source_type = "http"
        config = session.get(SubscriptionConfigVersion, batch_world["config"])
        config.config = {
            "source_type": "http",
            "mode": "business_batch",
            "ordering": "receipt_serial",
            "limits": {"rate": 1000},
        }
    return batch_world


def mq_input(world, index, *, message_id=None, body=b"test"):
    envelope = queue_envelope(world, message_id=message_id or f"message-{index}", body=body)
    return T1Input(
        index,
        envelope["event"]["id"],
        envelope,
        {"message_id": envelope["source"]["position"]["message_id"]},
        raw_size=len(body),
    )


def http_input(world, index, *, key=None, payload=None):
    key = key or f"request-{index}"
    payload = {"value": index} if payload is None else payload
    raw = json.dumps(payload, separators=(",", ":")).encode()
    envelope = {
        "schema": "dlr.event.v1",
        "subscription": {"id": str(world["sub"]), "config_version": 1, "source_epoch": "epoch-1"},
        "event": {"id": key, "kind": "message", "object_key": None},
        "source": {
            "type": "http",
            "identity": "unit-route",
            "position": {"idempotency_key": key},
            "attributes": {"public_id": "unit-route"},
        },
        "received_at": "2026-10-26T00:00:00Z",
        "data": {"encoding": "json", "content_type": "application/json", "payload": payload},
        "sync": None,
    }
    return T1Input(index, key, envelope, {"idempotency_key": key}, raw_body=raw)


def unit(session, world, source="rabbitmq", **extra):
    return T1Unit(
        session,
        world["sub"],
        source_type=source,
        source_epoch="epoch-1",
        shard="messages" if source == "rabbitmq" else "requests",
        owner_id="owner-a" if source == "rabbitmq" else None,
        generation=world["generation"] if source == "rabbitmq" else None,
        **extra,
    )


def facts(session, world):
    assert delivery.audit_quota(session, world["sub"])["differences"] == {}
    assert delivery.audit_global_quota(session)["differences"] == {}
    return session.get(SubscriptionQuota, f"sub:{world['sub']}")


@pytest.mark.parametrize("count", [4, 16])
def test_items_are_independent_receipts_and_one_bulk_insert_per_table(
    batch_world, session_factory, count
):
    with session_factory.begin() as session:
        assert ingress_capacity.activate_profile(session)
    statements = []

    def query(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", query)
    try:
        with session_factory.begin() as session:
            result = unit(session, batch_world).receive(
                [mq_input(batch_world, i) for i in range(count)]
            )
            assert [item.status_code for item in result.outcomes] == [202] * count
            assert len({item.receipt_id for item in result.outcomes}) == count
            assert result.accepted_prefix_count == count and result.stopped_index is None
            assert all(item.status["processing_status"] == "pending" for item in result.outcomes)
            ids = [item.receipt_id for item in result.outcomes]
    finally:
        event.remove(engine, "before_cursor_execute", query)
    assert sum(sql.startswith("INSERT INTO event_receipts ") for sql in statements) == 1
    assert sum(sql.startswith("INSERT INTO event_deliveries ") for sql in statements) == 1
    assert sum("FROM ingress_capacity_configurations" in sql for sql in statements) == 1
    assert sum("count(" in sql and "FROM source_checkpoints" in sql for sql in statements) == 1
    assert (
        sum(
            "clock_timestamp()" in sql and "subscription_sync_batches.ready_kind" in sql
            for sql in statements
        )
        == count
    )
    with session_factory() as session:
        quota = facts(session, batch_world)
        assert quota.pending_count == quota.history_count == quota.dedupe_count == count
        rows = session.scalars(select(EventReceipt).order_by(EventReceipt.sequence)).all()
        assert [row.id for row in rows] == ids
        assert [row.sequence for row in rows] == list(range(1, count + 1))
        assert sum(row.payload_bytes for row in rows) == quota.history_bytes
        assert all(row.source_raw_body is None for row in rows)


def test_queue_group_duplicate_and_conflict_commit_only_valid_prefix(batch_world, session_factory):
    first = mq_input(batch_world, 0, message_id="same", body=b"original")
    same = replace(first, index=1)
    conflict = mq_input(batch_world, 2, message_id="same", body=b"different")
    with session_factory.begin() as session:
        result = unit(session, batch_world).receive(
            [first, same, conflict, mq_input(batch_world, 3)]
        )
        assert [out.status_code for out in result.outcomes] == [202, 200, 409]
        assert result.accepted_prefix_count == 2 and result.stopped_index == 2
        assert result.outcomes[0].receipt_id == result.outcomes[1].receipt_id
    with session_factory() as session:
        assert facts(session, batch_world).pending_count == 1
        receipt = session.scalar(select(EventReceipt))
        assert receipt.duplicate_count == 1
        assert receipt.envelope == first.envelope
        assert session.scalar(select(SourceCheckpoint)).position == first.position


def test_http_content_and_identity_errors_do_not_block_valid_followers(http_world, session_factory):
    first = http_input(http_world, 0, key="same")
    bad = T1Input(
        1,
        "bad-json",
        None,
        {"idempotency_key": "bad-json"},
        raw_body=b"{",
        error=domain_error(422, "ingress_json_invalid", "invalid"),
    )
    conflict = http_input(http_world, 2, key="same", payload={"changed": True})
    last = http_input(http_world, 3)
    with session_factory.begin() as session:
        result = unit(session, http_world, "http").receive([first, bad, conflict, last])
        assert [out.status_code for out in result.outcomes] == [202, 422, 409, 202]
        assert result.stopped_index is None
    with session_factory() as session:
        assert facts(session, http_world).pending_count == 2
        assert session.scalar(select(SourceCheckpoint)).blocked_unaccepted is None
        rows = session.scalars(select(EventReceipt).order_by(EventReceipt.sequence)).all()
        assert [row.source_event_id for row in rows] == [first.identity, last.identity]
        assert rows[0].source_raw_body == first.raw_body
        assert rows[0].content_hash == hashlib.sha256(first.raw_body).hexdigest()


def test_http_same_group_duplicate_has_committed_202_200_identity(http_world, session_factory):
    first = http_input(http_world, 0, key="same")
    with session_factory.begin() as session:
        result = unit(session, http_world, "http").receive([first, replace(first, index=1)])
        assert [out.status_code for out in result.outcomes] == [202, 200]
        assert result.outcomes[0].receipt_id == result.outcomes[1].receipt_id
    with session_factory() as session:
        assert session.scalar(select(EventReceipt)).duplicate_count == 1
        assert facts(session, http_world).pending_count == 1


def test_http_existing_duplicate_precedes_feature_lease_capacity_and_new_parser_error(
    http_world, session_factory, monkeypatch
):
    first = http_input(http_world, 0)
    with session_factory.begin() as session:
        receipt_id = unit(session, http_world, "http").receive([first]).outcomes[0].receipt_id
    with session_factory.begin() as session:
        session.get(SubscriptionLease, http_world["sub"]).lease_until = database_now(
            session
        ) - timedelta(seconds=1)
        session.get(EventSubscription, http_world["sub"]).desired_state = "disabled"
        session.get(SubscriptionQuota, f"sub:{http_world['sub']}").blocked_reason = "full"
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    monkeypatch.setattr(settings, "http_ingress_enabled", False)
    with session_factory.begin() as session:
        result = unit(session, http_world, "http").receive(
            [
                replace(
                    first,
                    envelope=None,
                    error=domain_error(422, "ingress_json_invalid", "new parser"),
                )
            ]
        )
        assert result.outcomes[0].status_code == 200
        assert result.outcomes[0].receipt_id == receipt_id
        assert facts(session, http_world).pending_count == 1


def test_queue_existing_duplicate_still_requires_current_owner(batch_world, session_factory):
    first = mq_input(batch_world, 0)
    with session_factory.begin() as session:
        unit(session, batch_world).receive([first])
    with session_factory.begin() as session:
        session.get(SubscriptionLease, batch_world["sub"]).lease_until = database_now(
            session
        ) - timedelta(seconds=1)
    with pytest.raises(HTTPException) as failure, session_factory.begin() as session:
        unit(session, batch_world).receive([first])
    assert failure.value.detail["code"] == "subscription_owner_stale"
    with session_factory() as session:
        assert session.scalar(select(EventReceipt)).duplicate_count == 0


@pytest.mark.parametrize("elapsed_microseconds", [999999, 1000000])
def test_queue_capacity_failure_keeps_prefix_and_does_not_spend_rejected_local_rate(
    batch_world, session_factory, monkeypatch, elapsed_microseconds
):
    with session_factory.begin() as session:
        assert ingress_capacity.activate_profile(session)
        global_quota = session.get(SubscriptionQuota, "global")
        global_quota.rate_count = 999
        baseline = database_now(session)
        global_quota.rate_window_at = baseline
    fixed_now = baseline + timedelta(microseconds=elapsed_microseconds)
    monkeypatch.setattr(T1Unit, "_clock", lambda _self: (fixed_now, False))
    expected = 1 if elapsed_microseconds < 1000000 else 3
    with session_factory.begin() as session:
        result = unit(session, batch_world).receive([mq_input(batch_world, i) for i in range(3)])
        assert [out.status_code for out in result.outcomes] == (
            [202, 429] if expected == 1 else [202, 202, 202]
        )
        assert result.accepted_prefix_count == expected
        assert result.stopped_index == (1 if expected == 1 else None)
    with session_factory() as session:
        local = facts(session, batch_world)
        global_quota = session.get(SubscriptionQuota, "global")
        assert local.pending_count == local.history_count == local.dedupe_count == expected
        assert local.rate_count == expected and local.rate_window_at == fixed_now
        assert global_quota.rate_count == (1000 if expected == 1 else 3)
        assert global_quota.rate_window_at == (baseline if expected == 1 else fixed_now)
        receipts = session.scalars(select(EventReceipt).order_by(EventReceipt.sequence)).all()
        assert len(receipts) == expected
        assert (
            local.pending_bytes == local.history_bytes == sum(row.payload_bytes for row in receipts)
        )


def test_large_http_rejection_does_not_prevent_smaller_follower(http_world, session_factory):
    small = http_input(http_world, 0, payload="x")
    small_budget = accounting.batch_budget(small.envelope, len(small.raw_body)).total_bytes
    with session_factory.begin() as session:
        config = session.get(SubscriptionConfigVersion, http_world["config"])
        config.config = config.config | {
            "limits": {"pending_bytes": small_budget * 2, "rate": 1000}
        }
    with session_factory.begin() as session:
        result = unit(session, http_world, "http").receive(
            [
                small,
                http_input(http_world, 1, payload="x" * 1000),
                http_input(http_world, 2, payload="x"),
            ]
        )
        assert [out.status_code for out in result.outcomes] == [202, 429, 202]
    with session_factory() as session:
        assert facts(session, http_world).pending_bytes == small_budget * 2


def test_stop_before_shared_commit_rolls_back_every_new_item(
    batch_world, session_factory, monkeypatch
):
    stopped = []
    original = service.require_disk_capacity

    def disk():
        original()
        stopped.append(True)

    monkeypatch.setattr(service, "require_disk_capacity", disk)
    with pytest.raises(HTTPException), session_factory.begin() as session:
        unit(session, batch_world, stop_requested=lambda: bool(stopped)).receive(
            [mq_input(batch_world, i) for i in range(2)]
        )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0


def test_bulk_delivery_insert_failure_cannot_leave_receipts_or_counters(
    batch_world, session_factory
):
    engine = session_factory.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO event_deliveries "):
            raise RuntimeError("injected between receipt and delivery bulk insertion")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError), session_factory.begin() as session:
            unit(session, batch_world).receive([mq_input(batch_world, i) for i in range(4)])
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == 0
        assert delivery.audit_quota(session, batch_world["sub"])["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}


@pytest.mark.parametrize("kind", ["count", "bytes"])
def test_unit_bounds_are_checked_before_any_custody(batch_world, session_factory, kind):
    inputs = (
        [mq_input(batch_world, i) for i in range(17)]
        if kind == "count"
        else [mq_input(batch_world, i, body=b"x" * 170000) for i in range(3)]
    )
    with pytest.raises(HTTPException) as failure, session_factory.begin() as session:
        unit(session, batch_world).receive(inputs)
    assert failure.value.detail["code"] == "t1_unit_bounds_exceeded"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0


@pytest.mark.parametrize("extra_byte", [0, 1])
def test_exact_one_mib_unit_boundary_uses_five_individually_legal_envelopes(
    batch_world, session_factory, extra_byte
):
    inputs = [mq_input(batch_world, i, body=b"x" * 89000) for i in range(5)]
    inputs[-1].envelope["source"]["attributes"]["properties"]["padding"] = ""
    target = 1024**2 + extra_byte
    padding = target - sum(
        accounting.compact_bytes(item.envelope) + item.raw_size for item in inputs
    )
    assert 0 <= padding < 64 * 1024
    inputs[-1].envelope["source"]["attributes"]["properties"]["padding"] = "x" * padding
    envelope_sizes = [validate_envelope(item.envelope)[1] for item in inputs]
    raw_sizes = [
        len(base64.b64decode(item.envelope["data"]["value"], validate=True)) for item in inputs
    ]
    assert raw_sizes == [item.raw_size for item in inputs] == [89000] * 5
    assert all(size < 256 * 1024 for size in envelope_sizes)
    assert all(accounting.batch_budget(item.envelope).input_bytes < 256 * 1024 for item in inputs)
    assert sum(envelope_sizes) + sum(raw_sizes) == target
    columns = (
        "scope",
        "pending_count",
        "pending_bytes",
        "history_count",
        "history_bytes",
        "dedupe_count",
        "rate_count",
        "rate_window_at",
    )
    with session_factory.begin() as session:
        assert ingress_capacity.activate_profile(session)
        before = [
            tuple(getattr(row, column) for column in columns)
            for row in session.scalars(select(SubscriptionQuota).order_by(SubscriptionQuota.scope))
        ]
    if extra_byte:
        with pytest.raises(HTTPException) as failure, session_factory.begin() as session:
            unit(session, batch_world).receive(inputs)
        assert failure.value.detail["code"] == "t1_unit_bounds_exceeded"
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
            assert session.scalar(select(func.count()).select_from(EventDelivery)) == 0
            assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 0
            assert [
                tuple(getattr(row, column) for column in columns)
                for row in session.scalars(
                    select(SubscriptionQuota).order_by(SubscriptionQuota.scope)
                )
            ] == before
            assert delivery.audit_quota(session, batch_world["sub"])["differences"] == {}
            assert delivery.audit_global_quota(session)["differences"] == {}
    else:
        with session_factory.begin() as session:
            result = unit(session, batch_world).receive(inputs)
            assert [out.status_code for out in result.outcomes] == [202] * 5
            assert result.accepted_prefix_count == 5 and result.stopped_index is None
        with session_factory() as session:
            quota = facts(session, batch_world)
            assert quota.pending_count == quota.history_count == quota.dedupe_count == 5
            assert (
                quota.pending_bytes
                == quota.history_bytes
                == sum(accounting.batch_budget(item.envelope).total_bytes for item in inputs)
            )
            assert quota.rate_count == session.get(SubscriptionQuota, "global").rate_count == 5
            assert session.scalar(select(func.count()).select_from(EventDelivery)) == 5
            assert session.scalar(select(SourceCheckpoint)).position == inputs[-1].position


def test_http_unit_rejects_seventeen_requests_before_any_custody(http_world, session_factory):
    with pytest.raises(HTTPException) as failure, session_factory.begin() as session:
        unit(session, http_world, "http").receive([http_input(http_world, i) for i in range(17)])
    assert failure.value.detail["code"] == "t1_unit_bounds_exceeded"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0


def test_unit_cannot_survive_its_transaction_or_be_reused(batch_world, session_factory):
    with session_factory() as session:
        current = unit(session, batch_world)
        current.receive([mq_input(batch_world, 0)])
        with pytest.raises(HTTPException):
            current.receive([mq_input(batch_world, 1)])
        session.commit()
        with pytest.raises(HTTPException):
            current.receive([mq_input(batch_world, 2)])


def test_ready_control_appearing_after_first_item_stops_next_token(
    batch_world, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        adapter = Adapter(name="separate-control-source", language="python", adapter_type="task")
        session.add(adapter)
        session.flush()
        sub = EventSubscription(
            adapter_id=adapter.id, source_type="kubernetes", source_epoch="control-epoch"
        )
        session.add(sub)
        session.flush()
        control = SyncBatch(
            subscription_id=sub.id,
            source_epoch=sub.source_epoch,
            scope_fingerprint="f" * 64,
            state="processing",
        )
        session.add(control)
        session.flush()
        control_id = control.id
    original = T1Unit._clock
    clocks = []

    def clock(current):
        clocks.append(True)
        if len(clocks) == 2:
            with session_factory.begin() as other:
                pending = other.get(SyncBatch, control_id)
                pending.ready_kind = "sync.failed"
                pending.ready_since = database_now(other)
        return original(current)

    monkeypatch.setattr(T1Unit, "_clock", clock)
    with session_factory.begin() as session:
        result = unit(session, batch_world).receive([mq_input(batch_world, i) for i in range(3)])
        assert [out.status_code for out in result.outcomes] == [202, 429]
        assert result.accepted_prefix_count == 1
    with session_factory() as session:
        assert facts(session, batch_world).pending_count == 1


def test_disk_is_checked_for_each_new_item_and_rejection_keeps_only_accepted_budget(
    batch_world, session_factory, monkeypatch
):
    calls = []

    def disk():
        calls.append(True)
        if len(calls) == 2:
            raise domain_error(503, "ingress_disk_low", "low")

    monkeypatch.setattr(service, "require_disk_capacity", disk)
    with session_factory.begin() as session:
        result = unit(session, batch_world).receive([mq_input(batch_world, i) for i in range(3)])
        assert [out.status_code for out in result.outcomes] == [202, 503]
        assert result.outcomes[1].error.detail["code"] == "ingress_disk_low"
    assert len(calls) == 2
    with session_factory() as session:
        assert facts(session, batch_world).pending_count == 1


def test_binding_mutation_is_not_hidden_by_same_session_state(batch_world, session_factory):
    with pytest.raises(HTTPException) as failure, session_factory.begin() as session:
        current = unit(session, batch_world)
        current.subscription.config_version += 1
        current.receive([mq_input(batch_world, 0)])
    assert failure.value.detail["code"] == "subscription_config_conflict"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        assert session.get(EventSubscription, batch_world["sub"]).config_version == 1


def test_lease_expiry_before_shared_commit_prevents_partial_custody(
    batch_world, session_factory, monkeypatch
):
    original = T1Unit._reserve

    def reserve(current, size, now, ready):
        values = original(current, size, now, ready)
        current._lease_row.lease_until = database_now(current.session) - timedelta(seconds=1)
        return values

    monkeypatch.setattr(T1Unit, "_reserve", reserve)
    with pytest.raises(HTTPException) as failure, session_factory.begin() as session:
        unit(session, batch_world).receive([mq_input(batch_world, i) for i in range(2)])
    assert failure.value.detail["code"] == "subscription_owner_stale"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0


def test_reused_clock_statement_reads_fresh_database_time_and_control_state(
    batch_world, session_factory
):
    from sqlalchemy import text

    with session_factory.begin() as session:
        clock_unit = unit(session, batch_world)
        first, ready = clock_unit._clock()
        assert not ready
        session.execute(text("SELECT pg_sleep(0.01)"))
        second, after = clock_unit._clock()
        assert second > first and not after
