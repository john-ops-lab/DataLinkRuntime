"""The W1-B increment preserves old rows and refuses to erase recovery facts."""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from dlr.common.schema_version import SCHEMA_REVISION
from test_issue219_upgrade import migration_config, upgrade_database  # noqa: F401


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
    command.upgrade(config, "head")
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
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
        assert (
            connection.scalar(
                text("SELECT identity_id FROM workers WHERE id=:id"), {"id": legacy.id}
            )
            == legacy.identity_id
        )
        assert connection.scalar(text("SELECT count(*) FROM worker_maintenance_operations")) == (
            guard == "audit"
        )
