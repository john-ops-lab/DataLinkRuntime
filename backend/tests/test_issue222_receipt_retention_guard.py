"""PostgreSQL rejects nullable TTL bypasses without releasing shared batch input."""

from copy import deepcopy
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from dlr.control.models import EventReceipt, Execution, SubscriptionExecutionBatch
from test_issue219_upgrade import migration_config
from test_issue222_business_batch import batch_world as core_batch_world
from test_issue222_business_batch import finish, produce
from test_issue222_business_batch import world as foundation_world

world = foundation_world
batch_world = core_batch_world
OLD_REVISION = "0053_issue222_batch_guard_json"
NEW_REVISION = "0054_issue222_receipt_ttl_guard"


@pytest.fixture(autouse=True)
def install_guard_revision(session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # An earlier migration-specific test may intentionally leave the shared
    # test engine at 0053. These regressions exercise the new deployed guard.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(migration_config(session_factory.kw["bind"]), NEW_REVISION)


def retained_snapshot(factory: Any) -> dict[str, Any]:
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
            table: list(
                session.scalars(text(f"SELECT row_to_json(t) FROM {table} t ORDER BY {key}"))
            )
            for table, key in tables.items()
        }


@pytest.mark.parametrize("terminal_time", ["NULL", "now()-interval '6 days'"])
def test_same_update_cannot_clear_payload_without_elapsed_ttl(
    batch_world: Any, session_factory: Any, terminal_time: str
) -> None:
    with session_factory.begin() as session:
        ids, _ = produce(session, batch_world, 2)
    before = retained_snapshot(session_factory)
    # Both state and nullable terminal time change in the very statement that
    # removes the independent Envelope. No Python cleanup validation runs.
    with pytest.raises(IntegrityError) as rejected, session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE event_receipts SET state='succeeded', envelope=NULL, "
                f"terminal_at={terminal_time} WHERE id=:identity"
            ),
            {"identity": ids[0]},
        )
    assert rejected.value.orig.sqlstate == "23514"
    assert (
        rejected.value.orig.diag.message_primary == "batch receipt payload is immutable before TTL"
    )
    assert retained_snapshot(session_factory) == before


def test_elapsed_ttl_can_clear_receipt_without_clearing_shared_input(
    batch_world: Any, session_factory: Any
) -> None:
    with session_factory.begin() as session:
        ids, execution = produce(session, batch_world, 2)
        finish(session, execution)
        execution_id = execution.id
        original_input = deepcopy(execution.input)
        original_manifest = deepcopy(session.get(SubscriptionExecutionBatch, execution_id).manifest)
        other_envelope = deepcopy(session.get(EventReceipt, ids[1]).envelope)
    before = retained_snapshot(session_factory)
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE event_receipts SET state='succeeded', terminal_at=now()-interval '7 days', "
                "envelope=NULL WHERE id=:identity"
            ),
            {"identity": ids[0]},
        )
    with session_factory() as session:
        assert session.get(EventReceipt, ids[0]).envelope is None
        assert session.get(EventReceipt, ids[1]).envelope == other_envelope
        assert session.get(Execution, execution_id).input == original_input
        assert session.get(SubscriptionExecutionBatch, execution_id).manifest == original_manifest
    after = retained_snapshot(session_factory)
    # The guard grants no accounting release, membership deletion, or shared
    # input cleanup. Those remain separate existing transaction rules.
    for table in before.keys() - {"event_receipts"}:
        assert after[table] == before[table]


def function_identity(factory: Any) -> tuple[Any, list[Any]]:
    with factory() as session:
        function = session.execute(
            text(
                "SELECT oid,proowner,proacl,prosecdef,provolatile,proparallel,proleakproof,prosrc "
                "FROM pg_proc WHERE oid='dlr_batch_immutable()'::regprocedure"
            )
        ).one()
        triggers = list(
            session.execute(
                text(
                    "SELECT oid,tgrelid,tgfoid,tgtype,tgenabled FROM pg_trigger "
                    "WHERE tgfoid='dlr_batch_immutable()'::regprocedure ORDER BY oid"
                )
            )
        )
        return function, triggers


def test_0054_upgrade_and_downgrade_preserve_custody_identity_permissions_and_bindings(
    batch_world: Any, session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = migration_config(session_factory.kw["bind"])
    command.downgrade(config, OLD_REVISION)
    try:
        with session_factory.begin() as session:
            produce(session, batch_world, 2)
        before = retained_snapshot(session_factory)
        old_function, old_triggers = function_identity(session_factory)
        command.upgrade(config, NEW_REVISION)
        new_function, new_triggers = function_identity(session_factory)
        assert new_function[:-1] == old_function[:-1]
        assert new_triggers == old_triggers
        assert new_function.prosrc == old_function.prosrc.replace(
            "AND NEW.terminal_at<=", "AND NEW.terminal_at IS NOT NULL AND NEW.terminal_at<=", 1
        )
        assert retained_snapshot(session_factory) == before
        with session_factory() as session:
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == NEW_REVISION
        command.downgrade(config, OLD_REVISION)
        assert function_identity(session_factory) == (old_function, old_triggers)
        assert retained_snapshot(session_factory) == before
    finally:
        command.upgrade(config, NEW_REVISION)
