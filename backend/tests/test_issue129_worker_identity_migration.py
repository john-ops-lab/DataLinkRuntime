"""Worker identity migration preserves legacy bindings and protects new state."""

import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from dlr.common.schema_version import SCHEMA_REVISION
from test_issue219_upgrade import (  # noqa: F401
    insert_legacy_adapter,
    migration_config,
    upgrade_database,
)

PREVIOUS = "0054_issue222_receipt_ttl_guard"
CURRENT = "0055_issue129_worker_identity"
CLEANUP_INTENT = "0085_issue129_cleanup_intent"


def test_complete_stage_chain_preserves_legacy_history_and_refuses_downgrade(
    upgrade_database: Engine,  # noqa: F811
) -> None:
    config = migration_config(upgrade_database)
    command.upgrade(config, PREVIOUS)
    with upgrade_database.begin() as conn:
        worker = conn.scalar(
            text(
                "INSERT INTO workers (name,status,capabilities) "
                "VALUES ('stage-legacy','offline','[\"python\"]') RETURNING id"
            )
        )
        adapter = conn.scalar(
            text(
                "INSERT INTO adapters (name,language,adapter_type,run_mode,runtime_worker_id) "
                "VALUES ('stage-history','python','task','manual',:worker) RETURNING id"
            ),
            {"worker": worker},
        )
        version = conn.scalar(
            text(
                "INSERT INTO adapter_versions (adapter_id,seq,code) "
                "VALUES (:adapter,1,'pass') RETURNING id"
            ),
            {"adapter": adapter},
        )
        execution = conn.scalar(
            text(
                "INSERT INTO executions (adapter_id,version_id,trigger,status,input,"
                "input_source_type,input_config_revision,input_snapshot) "
                "VALUES (:adapter,:version,'manual','succeeded','{\"kept\":[null,false,42]}',"
                "'json',7,'{}') RETURNING id"
            ),
            {"adapter": adapter, "version": version},
        )
        attempt = conn.scalar(
            text(
                "INSERT INTO execution_attempts (execution_id,adapter_id,attempt_no,worker_id,"
                "fencing_token,lease_expires_at,status,claimed_at) "
                "VALUES (:execution,:adapter,1,:worker,3,now(),'succeeded',now()) RETURNING id"
            ),
            {"execution": execution, "adapter": adapter, "worker": worker},
        )
        query = text(
            "SELECT a.runtime_worker_id,e.id,e.version_id,e.status,e.input,e.input_config_revision,"
            "t.id,t.worker_id,t.fencing_token,t.status FROM adapters a JOIN executions e "
            "ON e.adapter_id=a.id JOIN execution_attempts t ON t.execution_id=e.id "
            "WHERE t.id=:attempt"
        )
        before = conn.execute(query, {"attempt": attempt}).one()
    command.upgrade(config, CLEANUP_INTENT)
    with pytest.raises(
        RuntimeError, match="Worker cleanup intent responsibilities cannot be safely downgraded"
    ):
        command.downgrade(config, PREVIOUS)
    with upgrade_database.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == CLEANUP_INTENT
        assert conn.execute(query, {"attempt": attempt}).one() == before

    command.upgrade(config, "head")
    with upgrade_database.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
        assert conn.execute(query, {"attempt": attempt}).one() == before
        old = conn.execute(
            text(
                "SELECT identity_id,identity_enrolled_at,protocol_version FROM workers WHERE id=:id"
            ),
            {"id": worker},
        ).one()
        assert isinstance(old.identity_id, uuid.UUID)
        assert old.identity_enrolled_at is None and old.protocol_version == 3
        assert conn.execute(
            text(
                "SELECT worker_identity_id,worker_instance_id,worker_instance_generation,"
                "cleanup_settled_at FROM execution_attempts WHERE id=:id"
            ),
            {"id": attempt},
        ).one() == (None, None, None, None)
        for table in (
            "worker_lease_candidates_v4",
            "attempt_lease_candidates_v4",
            "attempt_result_intents_v4",
            "worker_cleanup_intents_v4",
        ):
            assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0


def test_upgrade_backfills_distinct_identity_and_preserves_worker_binding(
    upgrade_database: Engine,  # noqa: F811
) -> None:
    config = migration_config(upgrade_database)
    command.upgrade(config, PREVIOUS)
    with upgrade_database.begin() as connection:
        first = connection.scalar(
            text(
                "INSERT INTO workers (name, status, capabilities) "
                "VALUES ('old-a', 'online', '[\"python\"]') RETURNING id"
            )
        )
        second = connection.scalar(
            text(
                "INSERT INTO workers (name, status, capabilities) "
                "VALUES ('old-b', 'offline', '[\"go\"]') RETURNING id"
            )
        )
    with Session(upgrade_database) as session, session.begin():
        adapter_id = insert_legacy_adapter(session, "existing", runtime_worker_id=first)

    command.upgrade(config, CURRENT)
    with upgrade_database.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT id, name, identity_id, identity_enrolled_at, instance_id, "
                    "instance_generation, instance_lease_until, scheduling_state, capacity_state "
                    "FROM workers ORDER BY id"
                )
            )
            .mappings()
            .all()
        )
        assert [row["id"] for row in rows] == [first, second]
        assert [row["name"] for row in rows] == ["old-a", "old-b"]
        assert all(isinstance(row["identity_id"], uuid.UUID) for row in rows)
        assert rows[0]["identity_id"] != rows[1]["identity_id"]
        for row in rows:
            assert row["identity_enrolled_at"] is None
            assert row["instance_id"] is None and row["instance_lease_until"] is None
            assert row["instance_generation"] == 0
            assert row["scheduling_state"] == "active" and row["capacity_state"] == "unknown"
        assert (
            connection.scalar(
                text("SELECT runtime_worker_id FROM adapters WHERE id=:id"), {"id": adapter_id}
            )
            == first
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO workers (name, status, capabilities, identity_id) "
                    "VALUES ('duplicate-identity', 'offline', '[]', :identity)"
                ),
                {"identity": rows[0]["identity_id"]},
            )
        connection.execute(
            text("UPDATE workers SET name='old-a-renamed', identity_id=identity_id WHERE id=:id"),
            {"id": first},
        )
        with (
            pytest.raises(IntegrityError, match="Worker identity is immutable"),
            connection.begin_nested(),
        ):
            connection.execute(
                text("UPDATE workers SET identity_id=:identity WHERE id=:id"),
                {"identity": uuid.uuid4(), "id": first},
            )
        with (
            pytest.raises(IntegrityError, match="Worker identity is immutable"),
            connection.begin_nested(),
        ):
            connection.execute(text("UPDATE workers SET id=id+100 WHERE id=:id"), {"id": first})
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        with (
            pytest.raises(IntegrityError, match="Worker identity is immutable"),
            connection.begin_nested(),
        ):
            connection.execute(
                text("UPDATE workers SET identity_id=:identity WHERE id=:id"),
                {"identity": uuid.uuid4(), "id": first},
            )
        connection.execute(text("SET LOCAL session_replication_role = origin"))
        enrollment_savepoint = connection.begin_nested()
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            text("UPDATE workers SET identity_enrolled_at=clock_timestamp() WHERE id=:id"),
            {"id": first},
        )
        with (
            pytest.raises(IntegrityError, match="Worker enrollment is immutable"),
            connection.begin_nested(),
        ):
            connection.execute(
                text("UPDATE workers SET identity_enrolled_at=NULL WHERE id=:id"), {"id": first}
            )
        with (
            pytest.raises(IntegrityError, match="Worker enrollment is immutable"),
            connection.begin_nested(),
        ):
            connection.execute(
                text("UPDATE workers SET identity_enrolled_at=clock_timestamp() WHERE id=:id"),
                {"id": first},
            )
        enrollment_savepoint.rollback()
        connection.execute(
            text("""
            CREATE FUNCTION dlr129_replace_identity() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN NEW.identity_id := gen_random_uuid(); RETURN NEW; END $$
        """)
        )
        connection.execute(
            text("""
            CREATE TRIGGER zz_dlr129_replace_identity BEFORE UPDATE ON workers
            FOR EACH ROW EXECUTE FUNCTION dlr129_replace_identity()
        """)
        )
        with (
            pytest.raises(IntegrityError, match="Worker identity is immutable"),
            connection.begin_nested(),
        ):
            connection.execute(
                text("UPDATE workers SET status='offline' WHERE id=:id"), {"id": first}
            )
        connection.execute(text("DROP TRIGGER zz_dlr129_replace_identity ON workers"))
        connection.execute(text("DROP FUNCTION dlr129_replace_identity()"))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE workers SET instance_id=:instance WHERE id=:id"),
                {"instance": uuid.uuid4(), "id": first},
            )
    command.downgrade(config, PREVIOUS)
    with upgrade_database.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT runtime_worker_id FROM adapters WHERE id=:id"), {"id": adapter_id}
            )
            == first
        )
        assert connection.scalar(text("SELECT count(*) FROM workers")) == 2


def test_credentials_store_digest_only_and_prevent_lossy_downgrade(
    upgrade_database: Engine,  # noqa: F811
) -> None:
    config = migration_config(upgrade_database)
    command.upgrade(config, CURRENT)
    with upgrade_database.begin() as connection:
        worker = connection.scalar(
            text(
                "INSERT INTO workers (name, status, capabilities) "
                "VALUES ('new', 'offline', '[]') RETURNING id"
            )
        )
        credential = connection.scalar(
            text(
                "INSERT INTO worker_credentials (worker_id, kind, secret_digest) "
                "VALUES (:worker, 'runtime', :digest) RETURNING id"
            ),
            {"worker": worker, "digest": b"a" * 32},
        )
        other = connection.scalar(
            text(
                "INSERT INTO workers (name, status, capabilities) "
                "VALUES ('other', 'offline', '[]') RETURNING id"
            )
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO worker_credential_audit "
                    "(worker_id, credential_record_id, event_kind, actor_kind) "
                    "VALUES (:worker, :credential, 'revoke', 'superadmin')"
                ),
                {"worker": other, "credential": credential},
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO worker_credentials (worker_id, kind, secret_digest) "
                    "VALUES (:worker, 'enrollment', :digest)"
                ),
                {"worker": worker, "digest": b"b" * 32},
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO worker_credentials (worker_id, kind, secret_digest) "
                    "VALUES (:worker, 'runtime', :digest)"
                ),
                {"worker": worker, "digest": b"a" * 32},
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO worker_credentials (worker_id, kind, secret_digest) "
                    "VALUES (:worker, 'runtime', :digest)"
                ),
                {"worker": worker, "digest": b"x"},
            )
    with pytest.raises(DatabaseError, match="worker identity state prevents downgrade"):
        command.downgrade(config, PREVIOUS)
    with upgrade_database.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT
        assert connection.scalar(text("SELECT count(*) FROM worker_credentials")) == 1
