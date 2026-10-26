"""Real migration preserves old responsibilities without rewriting input identities."""

import hashlib
import json

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterVersion,
    EventSubscription,
    Execution,
    SubscriptionConfigVersion,
    Worker,
)
from test_issue219_upgrade import migration_config
from test_issue219_upgrade import upgrade_database as database_fixture

upgrade_database = database_fixture


def snapshot(connection, table):
    return list(connection.scalars(text(f"SELECT row_to_json(t) FROM {table} t ORDER BY id")))


def test_upgrade_preserves_pending_failed_and_kubernetes_control(upgrade_database):
    config = migration_config(upgrade_database)
    command.upgrade(config, "0050_issue223_adapter_batches")
    with Session(upgrade_database) as session, session.begin():
        worker = Worker(name="upgrade-worker", status="online", capabilities=["python"])
        adapter = Adapter(name="old-subscription", language="python", adapter_type="task")
        session.add_all([worker, adapter])
        session.flush()
        revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(revision)
        session.flush()
        sub = EventSubscription(
            adapter_id=adapter.id,
            source_type="kubernetes",
            source_epoch="retained-epoch",
            desired_state="disabled",
        )
        session.add(sub)
        session.flush()
        cv = SubscriptionConfigVersion(
            subscription_id=sub.id,
            version=1,
            source_epoch=sub.source_epoch,
            config={"legacy": [None, False, 42]},
        )
        session.add(cv)
        session.flush()
        activation_id = session.scalar(
            text(
                "INSERT INTO subscription_activations "
                "(subscription_id,config_version_id,revision_id,worker_id) "
                "VALUES (:sub,:config,:revision,:worker) RETURNING id"
            ),
            {"sub": sub.id, "config": cv.id, "revision": revision.id, "worker": worker.id},
        )
        sub.activation_id = activation_id
        sub_id = sub.id
    with upgrade_database.begin() as connection:
        for index, (state, phase) in enumerate(
            [("pending", None), ("failed", None), ("pending", "sync.completed")], 1
        ):
            envelope = {"legacy": [None, False, index], "phase": phase}
            original = json.dumps(envelope, separators=(",", ":"))
            receipt = connection.scalar(
                text(
                    "INSERT INTO event_receipts (subscription_id,activation_id,source_epoch,"
                    "source_event_id,content_hash,envelope,payload_bytes,shard,sequence,"
                    "state,source_phase) VALUES (:sub,:activation,'retained-epoch',:identity,"
                    ":hash,CAST(:envelope AS jsonb),:size,'old-scope',:sequence,:state,:phase) "
                    "RETURNING id"
                ),
                {
                    "sub": sub_id,
                    "activation": activation_id,
                    "identity": f"legacy-{index}",
                    "hash": hashlib.sha256(original.encode()).hexdigest(),
                    "envelope": original,
                    "size": len(original),
                    "sequence": index,
                    "state": state,
                    "phase": phase,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO event_deliveries (receipt_id,ordinal,state) "
                    "VALUES (:receipt,1,:state)"
                ),
                {"receipt": receipt, "state": state},
            )
        connection.execute(
            text("""
            INSERT INTO subscription_quotas(scope,pending_count,pending_bytes,
                history_count,history_bytes,dedupe_count)
            SELECT :scope, 2,
                sum(payload_bytes) FILTER(WHERE state='pending'),
                3, sum(payload_bytes),3 FROM event_receipts
        """),
            {"scope": f"sub:{sub_id}"},
        )
        connection.execute(
            text("""
            INSERT INTO subscription_quotas(scope,pending_count,pending_bytes,
                history_count,history_bytes,dedupe_count)
            SELECT 'global',pending_count,pending_bytes,history_count,history_bytes,dedupe_count
            FROM subscription_quotas WHERE scope=:scope
        """),
            {"scope": f"sub:{sub_id}"},
        )
        before = {
            table: snapshot(connection, table)
            for table in [
                "event_subscriptions",
                "subscription_config_versions",
                "subscription_activations",
                "event_receipts",
                "event_deliveries",
            ]
        }
    command.upgrade(config, "head")
    with upgrade_database.connect() as connection:
        after = {table: snapshot(connection, table) for table in before}
        for row in after["event_receipts"]:
            assert row.pop("source_raw_body") is None
            row.pop("retained_payload_bytes")
            row.pop("retained_metadata_bytes")
            previous = next(value for value in before["event_receipts"] if value["id"] == row["id"])
            assert row["payload_bytes"] == previous["payload_bytes"] * 2 + 4608
            row["payload_bytes"] = previous["payload_bytes"]
        for row in after["event_deliveries"]:
            row.pop("retained_execution_bytes")
            row.pop("retained_metadata_bytes")
            assert row.pop("retained_batch_bytes") == 0
            assert row.pop("batch_execution_id") is None
            assert row.pop("batch_member_outcome") is None
            assert row.pop("batch_member_code") is None
        for row in after["subscription_activations"]:
            assert row.pop("execution_policy_snapshot") is None
        assert after == before
        from dlr.control.services.subscription_delivery import audit_global_quota, audit_quota

        with Session(bind=connection) as session:
            assert audit_quota(session, sub_id)["differences"] == {}
            assert audit_global_quota(session)["differences"] == {}
        assert connection.scalar(text("SELECT count(*) FROM http_ingress_routes")) == 0
        assert connection.scalar(text("SELECT count(*) FROM ingress_capacity_audits")) == 0
        assert connection.scalar(text("SELECT count(*) FROM ingress_capacity_configurations")) == 0
        assert (
            connection.scalar(
                text("SELECT count(*) FROM pg_indexes WHERE indexname='ix_receipt_unsettled_shard'")
            )
            == 1
        )


def test_fresh_http_schema_and_downgrade_preserve_permanent_route_marker(upgrade_database):
    config = migration_config(upgrade_database)
    command.upgrade(config, "head")
    with upgrade_database.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM http_ingress_routes")) == 0
        # A removed subscription leaves an unassigned marker; public IDs remain
        # unavailable forever even after permanent Adapter history deletion.
        connection.execute(
            text(
                "INSERT INTO http_ingress_routes (public_id,subscription_id,source_epoch) "
                "VALUES ('permanent-marker',NULL,'retired-epoch')"
            )
        )
    with pytest.raises(RuntimeError, match="Preserve ingress routes"):
        command.downgrade(config, "0050_issue223_adapter_batches")
    with upgrade_database.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "0054_issue222_receipt_ttl_guard"
        )
        assert (
            connection.scalar(
                text(
                    "SELECT source_epoch FROM http_ingress_routes "
                    "WHERE public_id='permanent-marker'"
                )
            )
            == "retired-epoch"
        )


@pytest.mark.parametrize("counter_surplus", [0, 137])
def test_migration_adds_old_execution_and_control_reserve_deltas_without_reset(
    upgrade_database, counter_surplus
):
    from dlr.control.services.subscription_delivery import audit_global_quota, audit_quota

    config = migration_config(upgrade_database)
    command.upgrade(config, "0050_issue223_adapter_batches")
    with Session(upgrade_database) as session, session.begin():
        adapter = Adapter(name="retained-copy", language="python", adapter_type="task")
        worker = Worker(name="old-worker", status="online", capabilities=["python"])
        session.add_all([adapter, worker])
        session.flush()
        version = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(version)
        session.flush()
        sub = EventSubscription(
            adapter_id=adapter.id, source_type="kubernetes", source_epoch="old-epoch"
        )
        session.add(sub)
        session.flush()
        cv = SubscriptionConfigVersion(
            subscription_id=sub.id, version=1, source_epoch="old-epoch", config={}
        )
        session.add(cv)
        session.flush()
        activation_id = session.scalar(
            text(
                "INSERT INTO subscription_activations(subscription_id,config_version_id,"
                "revision_id,worker_id) VALUES (:sub,:cv,:revision,:worker) RETURNING id"
            ),
            {"sub": sub.id, "cv": cv.id, "revision": version.id, "worker": worker.id},
        )
        # Build the genuine old schema row; the current ORM deliberately has
        # additional columns that are not present before this upgrade.
        execution_id = session.scalar(
            text(
                "INSERT INTO executions(adapter_id,version_id,trigger,status,input,locale,"
                "input_source_type,input_config_revision,input_snapshot) "
                "VALUES (:adapter,:version,'subscription','succeeded',"
                "CAST(:input AS jsonb),'zh-CN',"
                "'json',1,CAST(:snapshot AS jsonb)) "
                "RETURNING id"
            ),
            {
                "adapter": adapter.id,
                "version": version.id,
                "input": json.dumps({"legacy": [None, False, 42]}),
                "snapshot": json.dumps({"source_type": "json", "revision": 1}),
            },
        )
        sub_id = sub.id
    with upgrade_database.begin() as connection:
        receipt_id = connection.scalar(
            text("""
            INSERT INTO event_receipts(subscription_id,activation_id,source_epoch,
                source_event_id,content_hash,envelope,payload_bytes,shard,sequence,state,terminal_at)
            VALUES (:sub,:activation,'old-epoch','retained-id',:hash,NULL,123,'scope',1,
                'succeeded',now()-interval '8 days') RETURNING id
        """),
            {"sub": sub_id, "activation": activation_id, "hash": "b" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO event_deliveries(receipt_id,ordinal,state,execution_id) "
                "VALUES (:receipt,1,'succeeded',:execution)"
            ),
            {"receipt": receipt_id, "execution": execution_id},
        )
        connection.execute(
            text("""
            INSERT INTO subscription_sync_batches(subscription_id,scope_fingerprint,state,
                read_complete,activation_id,source_epoch,reserved_control_count,reserved_control_bytes)
            VALUES (:sub,:fingerprint,'processing',false,:activation,'old-epoch',3,24576)
        """),
            {"sub": sub_id, "activation": activation_id, "fingerprint": "a" * 64},
        )
        for scope in [f"sub:{sub_id}", "global"]:
            connection.execute(
                text("""
                INSERT INTO subscription_quotas(scope,history_bytes,dedupe_count,
                    rate_count,reserved_control_count,reserved_control_bytes)
                VALUES (:scope,:surplus,1,7,3,24576)
            """),
                {"scope": scope, "surplus": counter_surplus},
            )
        input_before = connection.scalar(
            text("SELECT input FROM executions WHERE id=:id"), {"id": execution_id}
        )
    command.upgrade(config, "head")
    with Session(upgrade_database) as session:
        assert session.get(Execution, execution_id).input == input_before
        assert session.get(Execution, execution_id).subscription_delivery_kind == "single"
        row = session.execute(
            text("SELECT source_event_id,content_hash,envelope FROM event_receipts WHERE id=:id"),
            {"id": receipt_id},
        ).one()
        assert row == ("retained-id", "b" * 64, None)
        local = audit_quota(session, sub_id)
        shared = audit_global_quota(session)
        for report in [local, shared]:
            assert report["actual"]["pending_count"] == report["actual"]["pending_bytes"] == 0
            assert report["actual"]["history_count"] == 1
            assert report["actual"]["reserved_control_count"] == 3
            assert report["actual"]["reserved_control_bytes"] == 3 * 20992
            assert report["actual"]["dedupe_count"] == 1
        assert (
            local["actual"]["history_bytes"] == local["expected"]["history_bytes"] + counter_surplus
        )
        assert set(local["differences"]) == ({"history_bytes"} if counter_surplus else set())
        # Global audit compares the retained local ledgers, so a matching
        # pre-existing surplus is preserved there rather than silently repaired.
        assert shared["actual"]["history_bytes"] == local["actual"]["history_bytes"]
        assert shared["differences"] == {}
        assert (
            session.scalar(text("SELECT rate_count FROM subscription_quotas WHERE scope='global'"))
            == 7
        )
