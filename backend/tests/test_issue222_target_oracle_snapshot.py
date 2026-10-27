"""Real PostgreSQL snapshot/read-only checks for the independent target oracle."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import Engine, text


@pytest.fixture
def target_oracle(test_engine: Engine, tmp_path: Path) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    name = "issue222_target_oracle_snapshot_workload"
    spec = importlib.util.spec_from_file_location(name, root / "tools/ingress/workload.py")
    assert spec is not None and spec.loader is not None
    workload = importlib.util.module_from_spec(spec)
    sys.modules[name] = workload
    spec.loader.exec_module(workload)
    config = {
        "kind": "warmup",
        "count": 20,
        "run_id": "oracle-snapshot-" + uuid4().hex,
        "input_path": str(tmp_path / "inputs.private.jsonl"),
        "output_path": str(tmp_path),
        "target_dsn": test_engine.url.set(drivername="postgresql").render_as_string(
            hide_password=False
        ),
    }
    workload.generate(config)
    inputs = workload.read_inputs(config)
    with test_engine.begin() as connection:
        identity = connection.execute(
            text("SELECT current_database(), oid FROM pg_database WHERE datname=current_database()")
        ).one()
        assert identity[0] == test_engine.url.database
        for statement in (root / "tools/ingress/target.sql").read_text().split(";"):
            if statement.strip():
                connection.execute(text(statement))
        for row in inputs:
            values = {
                "run_id": row["run_id"],
                "message_id": row["message_id"],
                "seq": row["seq"],
                "hash": row["payload_sha256"],
                "value": row["expected_normalized_value"],
            }
            connection.execute(
                text(
                    "INSERT INTO dlr_ingress_results "
                    "(run_id,message_id,seq,payload_sha256,normalized_value,attempts) "
                    "VALUES (:run_id,:message_id,:seq,:hash,:value,1)"
                ),
                values,
            )
            connection.execute(
                text(
                    "INSERT INTO dlr_ingress_attempts "
                    "(run_id,message_id,ordinal,outcome,payload_sha256,normalized_value,"
                    "handle_started_at,connected_at) "
                    "VALUES (:run_id,:message_id,1,'inserted',:hash,:value,"
                    "clock_timestamp(),clock_timestamp())"
                ),
                values,
            )
    return {"workload": workload, "config": config, "inputs": inputs, "identity": tuple(identity)}


def observe_real_cursors(
    monkeypatch: pytest.MonkeyPatch,
    target: dict[str, Any],
    after_results: Any = None,
) -> list[dict[str, Any]]:
    """Only observe real DECLARE completion; never synthesize rows or timing."""
    connect = psycopg.connect
    observed = []

    class ObservedCursor(psycopg.ServerCursor):
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            result = super().execute(*args, **kwargs)
            mode = self.connection.execute(
                "SELECT current_setting('transaction_isolation') AS isolation, "
                "current_setting('transaction_read_only') AS readonly"
            ).fetchone()
            observed.append({"cursor": self.name, **mode})
            if self.name == "b4_results" and after_results is not None:
                after_results()
            return result

    def reader(*args: Any, **kwargs: Any) -> Any:
        connection = connect(*args, **kwargs)
        try:
            actual = connection.execute(
                "SELECT current_database() AS database, oid "
                "FROM pg_database WHERE datname=current_database()"
            ).fetchone()
            assert (actual["database"], actual["oid"]) == target["identity"]
            connection.rollback()  # Keep verifier transaction settings legal at entry.
            connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            connection.read_only = False
            connection.server_cursor_factory = ObservedCursor
            return connection
        except BaseException:
            connection.close()
            raise

    monkeypatch.setattr(psycopg, "connect", reader)
    return observed


def test_verify_uses_one_snapshot_across_real_server_cursors(
    test_engine: Engine, target_oracle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    workload: ModuleType = target_oracle["workload"]
    config = target_oracle["config"]
    item = target_oracle["inputs"][0]
    committed = []
    with test_engine.connect() as writer:

        def commit_duplicate() -> None:
            if committed:
                return
            # The real results cursor already exists; attempts is not declared yet.
            with writer.begin():
                changed = writer.execute(
                    text(
                        "UPDATE dlr_ingress_results SET attempts=attempts+1 "
                        "WHERE run_id=:run AND message_id=:message RETURNING attempts"
                    ),
                    {"run": item["run_id"], "message": item["message_id"]},
                ).scalar_one()
                assert changed == 2
                writer.execute(
                    text(
                        "INSERT INTO dlr_ingress_attempts "
                        "(run_id,message_id,ordinal,outcome,payload_sha256,normalized_value,"
                        "handle_started_at,connected_at) "
                        "VALUES (:run,:message,2,'duplicate',:hash,:value,"
                        "clock_timestamp(),clock_timestamp())"
                    ),
                    {
                        "run": item["run_id"],
                        "message": item["message_id"],
                        "hash": item["payload_sha256"],
                        "value": item["expected_normalized_value"],
                    },
                )
            committed.append(True)  # Only after the second real transaction commits.

        observed = observe_real_cursors(monkeypatch, target_oracle, commit_duplicate)
        first = workload.verify(config)
        assert committed == [True]
        # Old READ COMMITTED mixes attempts=1 from results with two attempt rows.
        assert first["target_oracle_passed"], first["failure_counts"]
        assert (first["target_rows"], first["recorded_attempts"], first["effective_writes"]) == (
            20,
            20,
            20,
        )
        latest = workload.verify(config)
        assert latest["target_oracle_passed"], latest["failure_counts"]
        assert latest["recorded_attempts"] == 21
        assert committed == [True]
    assert [row["cursor"] for row in observed] == ["b4_results", "b4_attempts"] * 2
    assert all(
        row["isolation"] == "repeatable read" and row["readonly"] == "on" for row in observed
    )


def test_verify_read_only_snapshot_still_rejects_committed_bad_attempt_chain(
    test_engine: Engine, target_oracle: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    workload: ModuleType = target_oracle["workload"]
    config = target_oracle["config"]
    item = target_oracle["inputs"][0]
    with test_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE dlr_ingress_results SET attempts=2 "
                "WHERE run_id=:run AND message_id=:message"
            ),
            {"run": item["run_id"], "message": item["message_id"]},
        )
    observed = observe_real_cursors(monkeypatch, target_oracle)
    result = workload.verify(config)
    assert not result["target_oracle_passed"]
    assert result["failure_counts"] == {"attempt_accounting_mismatch": 1}
    assert (result["target_rows"], result["recorded_attempts"], result["effective_writes"]) == (
        20,
        20,
        20,
    )
    assert [row["cursor"] for row in observed] == ["b4_results", "b4_attempts"]
    assert all(
        row["isolation"] == "repeatable read" and row["readonly"] == "on" for row in observed
    )
