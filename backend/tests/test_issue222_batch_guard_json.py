"""The 0053 JSON expansion preserves real PostgreSQL batch custody and guards."""

from copy import deepcopy
from typing import Any

import pytest
from alembic import command
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    Execution,
    SubscriptionExecutionBatch,
    SubscriptionExecutionBatchMember,
)
from dlr.control.services import subscription_business_batch as batch
from test_issue219_upgrade import migration_config
from test_issue222_business_batch import batch_world as core_batch_world
from test_issue222_business_batch import facts, produce, receive
from test_issue222_business_batch import world as foundation_world

world = foundation_world
batch_world = core_batch_world


def migration(
    factory: Any, monkeypatch: pytest.MonkeyPatch, revision: str, *, down: bool = False
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = migration_config(factory.kw["bind"])
    (command.downgrade if down else command.upgrade)(config, revision)


def snapshot(factory: Any) -> dict[str, list[Any]]:
    tables = {
        "event_receipts": "id",
        "event_deliveries": "id",
        "executions": "id",
        "subscription_execution_batches": "execution_id",
        "subscription_execution_batch_members": "delivery_id",
        "subscription_quotas": "scope",
    }
    with factory() as session:
        return {
            name: list(session.scalars(text(f"SELECT row_to_json(t) FROM {name} t ORDER BY {key}")))
            for name, key in tables.items()
        }


def test_fresh_guard_keeps_hundred_large_members_and_all_trigger_bindings(
    batch_world: Any, session_factory: Any
) -> None:
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world, 100, b"x" * 1024)
        group = session.get(SubscriptionExecutionBatch, execution.id)
        assert len(ids) == group.member_count == 100
        assert 150000 < group.input_bytes <= 262144
        definition = session.scalar(
            text("SELECT pg_get_functiondef('dlr_batch_validate(bigint)'::regprocedure)")
        )
        assert definition.count("WITH ORDINALITY") == 2
        assert definition.count("AS MATERIALIZED") == 2
        assert "LEFT JOIN input_rows" in definition and "LEFT JOIN manifest_rows" in definition
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE tgname IN "
                    "('tr_delivery_owner','tr_batch_owner','tr_delivery_set_deferred',"
                    "'tr_execution_batch_deferred')"
                )
            )
            == 4
        )
        facts(session, batch_world)


def test_0052_upgrade_preserves_large_accepted_batch_and_validates_guard(
    batch_world: Any, session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    migration(session_factory, monkeypatch, "0052_issue222_business_batches", down=True)
    with session_factory.begin() as session:
        _, execution = produce(session, batch_world, 100, b"x" * 1024)
        execution_id = execution.id
        function_oid = session.scalar(
            text("SELECT 'dlr_batch_validate(bigint)'::regprocedure::oid")
        )
    before = snapshot(session_factory)
    with session_factory.begin() as session:
        session.execute(text("SELECT dlr_batch_validate(:identity)"), {"identity": execution_id})
    migration(session_factory, monkeypatch, "0053_issue222_batch_guard_json")
    assert snapshot(session_factory) == before
    with session_factory.begin() as session:
        session.execute(text("SELECT dlr_batch_validate(:identity)"), {"identity": execution_id})
        assert (
            session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0053_issue222_batch_guard_json"
        )
        assert (
            session.scalar(text("SELECT 'dlr_batch_validate(bigint)'::regprocedure::oid"))
            == function_oid
        )
        facts(session, batch_world)


@pytest.mark.parametrize(
    "mutation",
    [
        "input_receipt",
        "input_float_id",
        "input_ordinal",
        "input_index",
        "input_null_member",
        "input_envelope",
        "manifest_receipt",
        "manifest_hash",
        "manifest_null_member",
    ],
)
def test_last_of_hundred_invalid_members_is_rejected_without_losing_custody(
    batch_world: Any, session_factory: Any, mutation: str
) -> None:
    with session_factory.begin() as session:
        ids = [receive(session, batch_world, index, b"x" * 1024).id for index in range(100)]
        head = session.scalar(select(EventDelivery.id).where(EventDelivery.receipt_id == ids[0]))
    changed = False

    def corrupt_before_initial_insert(session: Any, _context: Any, _instances: Any) -> None:
        nonlocal changed
        for item in list(session.new):
            if changed:
                return
            if (
                mutation.startswith("input_")
                and isinstance(item, Execution)
                and item.subscription_delivery_kind == "batch"
            ):
                value = deepcopy(item.input)
                member = value["members"][-1]
                if mutation == "input_receipt":
                    member["receipt_id"] += 10000
                elif mutation == "input_float_id":
                    member["receipt_id"] = float(member["receipt_id"])
                elif mutation == "input_ordinal":
                    member["delivery_ordinal"] += 1
                elif mutation == "input_index":
                    member["member_index"] -= 1
                elif mutation == "input_null_member":
                    value["members"][-1] = None
                else:
                    member["input"]["data"]["value"] = "eQ=="
                item.input, changed = value, True
            elif mutation.startswith("manifest_") and isinstance(item, SubscriptionExecutionBatch):
                value = deepcopy(item.manifest)
                if mutation == "manifest_receipt":
                    value[-1]["receipt_id"] += 10000
                elif mutation == "manifest_hash":
                    value[-1]["content_hash"] = "f" * 64
                else:
                    value[-1] = None
                item.manifest, changed = value, True

    with pytest.raises(IntegrityError) as rejected, session_factory.begin() as session:
        event.listen(session, "before_flush", corrupt_before_initial_insert)
        execution = batch.materialize_batch(session, head)
        assert execution is not None
    assert changed
    assert rejected.value.orig.diag.message_primary == "batch member binding differs"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert (
            session.scalar(select(func.count()).select_from(SubscriptionExecutionBatchMember)) == 0
        )
        assert [session.get(EventReceipt, identity).state for identity in ids] == ["pending"] * 100
        assert facts(session, batch_world).pending_count == 100
