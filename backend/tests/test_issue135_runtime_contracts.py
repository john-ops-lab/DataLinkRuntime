"""Issue #135 runtime metadata and cancellation contracts."""

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine

from dlr.control.models import Worker


def test_worker_metadata_matches_published_migration_objects(test_engine: Engine) -> None:
    """The ORM describes the Worker objects already published by migration 0031."""
    worker_table = Worker.__table__
    metadata_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in worker_table.constraints
        if constraint.name is not None and hasattr(constraint, "sqltext")
    }
    metadata_indexes = {
        index.name: [column.name for column in index.columns] for index in worker_table.indexes
    }

    schema = inspect(test_engine)
    database_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in schema.get_check_constraints("workers")
    }
    database_indexes = {
        index["name"]: index["column_names"] for index in schema.get_indexes("workers")
    }

    expected_expression = "isolation_preflight_status IN ('unknown', 'passed', 'failed')"
    assert metadata_checks["ck_workers_isolation_preflight_status"] == expected_expression
    migrated_expression = database_checks["ck_workers_isolation_preflight_status"]
    assert "isolation_preflight_status" in migrated_expression
    assert all(value in migrated_expression for value in ("unknown", "passed", "failed"))
    expected_columns = ["protocol_version", "rabbitmq_execution_v3"]
    assert metadata_indexes["ix_workers_rabbitmq_execution_v3"] == expected_columns
    assert database_indexes["ix_workers_rabbitmq_execution_v3"] == expected_columns
    assert list(database_checks).count("ck_workers_isolation_preflight_status") == 1
    assert list(database_indexes).count("ix_workers_rabbitmq_execution_v3") == 1

    with test_engine.begin() as connection:
        worker_id = connection.scalar(
            Worker.__table__.insert()
            .values(
                name="issue135-existing-worker",
                status="offline",
                capabilities=["python"],
                isolation_preflight_status="passed",
                rabbitmq_execution_v3=True,
            )
            .returning(Worker.id)
        )
        preserved = connection.execute(
            select(
                Worker.name,
                Worker.status,
                Worker.isolation_preflight_status,
                Worker.rabbitmq_execution_v3,
            ).where(Worker.id == worker_id)
        ).one()
    assert preserved == ("issue135-existing-worker", "offline", "passed", True)
