"""The W1-B increment preserves old rows and refuses to erase recovery facts."""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from test_issue219_upgrade import migration_config, upgrade_database  # noqa: F401

RECOVERY = "0086_issue129_recovery"
DISPOSITION = "0087_issue129_disposition"


@pytest.mark.parametrize("guard", ["v4_identity", "audit"])
def test_recovery_increment_preserves_legacy_and_blocks_responsibility_downgrade(
    upgrade_database,  # noqa: F811
    guard,
):
    config = migration_config(upgrade_database)
    command.upgrade(config, "0085_issue129_cleanup_intent")
    with upgrade_database.begin() as connection:
        legacy = connection.execute(
            text(
                "INSERT INTO workers(name,status,capabilities) "
                "VALUES ('kept','offline','[\"python\"]') "
                "RETURNING id,identity_id,protocol_version,maintenance_version"
            )
        ).one()
    command.upgrade(config, RECOVERY)
    with upgrade_database.begin() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT id,identity_id,protocol_version,maintenance_version "
                    "FROM workers WHERE id=:id"
                ),
                {"id": legacy.id},
            ).one()
            == legacy
        )
        assert connection.scalar(text("SELECT count(*) FROM worker_maintenance_operations")) == 0
        if guard == "v4_identity":
            connection.execute(
                text("UPDATE workers SET protocol_version=4 WHERE id=:id"), {"id": legacy.id}
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO worker_maintenance_operations("
                    "request_id,worker_id,expected_version,"
                    "result_version,action,actor_kind,recovery_binding) "
                    "VALUES (:request,:worker,1,2,'reconcile','superadmin',"
                    "jsonb_build_object('fixture',true))"
                ),
                {"request": uuid4(), "worker": legacy.id},
            )
    with pytest.raises(DBAPIError, match="recovery provenance prevents downgrade"):
        command.downgrade(config, "0085_issue129_cleanup_intent")
    with upgrade_database.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == RECOVERY
        assert (
            connection.scalar(
                text("SELECT identity_id FROM workers WHERE id=:id"), {"id": legacy.id}
            )
            == legacy.identity_id
        )
        assert connection.scalar(text("SELECT count(*) FROM worker_maintenance_operations")) == (
            guard == "audit"
        )


def test_disposition_increment_preserves_old_audit_and_refuses_downgrade(
    upgrade_database,  # noqa: F811
):
    import json

    config = migration_config(upgrade_database)
    command.upgrade(config, RECOVERY)
    request = uuid4()
    with upgrade_database.begin() as connection:
        worker = connection.scalar(
            text(
                "INSERT INTO workers(name,status,capabilities) VALUES ('kept-c','offline','[]') "
                "RETURNING id"
            )
        )
        connection.execute(
            text(
                "INSERT INTO worker_maintenance_operations("
                "request_id,worker_id,expected_version,result_version,action,actor_kind,"
                "recovery_binding) "
                "VALUES (:request,:worker,1,2,'reconcile','superadmin','{}')"
            ),
            {"request": request, "worker": worker},
        )
    command.upgrade(config, DISPOSITION)
    with upgrade_database.begin() as connection:
        assert connection.execute(
            text(
                "SELECT recovery_disposition_id,recovery_disposition_xid,recovery_disposition "
                "FROM worker_maintenance_operations WHERE request_id=:request"
            ),
            {"request": request},
        ).one() == (None, None, None)
        snapshot = {
            "kind": "platform_terminal",
            "reason": "result_uncommitted_outcome_unknown",
            "acknowledgement": "result_uncommitted_outcome_unknown",
            "stopped_sha256": "a" * 64,
            "result_intent_id": str(uuid4()),
            "body_mac": "b" * 64,
            "attempt_status": "worker_lost",
            "execution_status": "cancelled",
            "dispatch_generation": 1,
            "attempt_no": 1,
        }
        statement = text(
            "INSERT INTO worker_maintenance_operations("
            "request_id,worker_id,expected_version,result_version,action,actor_kind,"
            "recovery_binding,recovery_disposition_id,recovery_disposition_xid,"
            "recovery_disposition) "
            "VALUES (:request,:worker,2,3,'reconcile','superadmin','{}',:disposition,:xid,"
            "CAST(:snapshot AS jsonb))"
        )
        params = {
            "request": uuid4(),
            "worker": worker,
            "disposition": uuid4(),
            "xid": connection.scalar(text("SELECT txid_current()")),
            "snapshot": json.dumps(snapshot),
        }
        for update in (
            {"xid": params["xid"] - 1},
            {"disposition": None},
            {"snapshot": json.dumps({**snapshot, "unexpected": True})},
            {"snapshot": json.dumps({**snapshot, "execution_status": "running"})},
            {"snapshot": json.dumps({**snapshot, "stopped_sha256": None})},
        ):
            with pytest.raises(DBAPIError), connection.begin_nested():
                connection.execute(statement, {**params, **update})
        connection.execute(statement, params)
    with pytest.raises(DBAPIError, match="disposition responsibility prevents downgrade"):
        command.downgrade(config, RECOVERY)
    with upgrade_database.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == DISPOSITION
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM worker_maintenance_operations "
                    "WHERE recovery_disposition_id IS NOT NULL"
                )
            )
            == 1
        )
