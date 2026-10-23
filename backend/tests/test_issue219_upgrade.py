"""Exercise the real upgrade over pre-subscription Adapter and input facts."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterInputConfig, AdapterVersion


@pytest.fixture
def upgrade_database(test_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    name = "dlr_219_upgrade_" + uuid.uuid4().hex[:12]
    maintenance = create_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with maintenance.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(test_engine.url.set(database=name))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        yield engine
    finally:
        engine.dispose()
        with maintenance.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        maintenance.dispose()


def migration_config(engine: Engine) -> Config:
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return config


def test_upgrade_preserves_saved_code_and_task_input(upgrade_database: Engine) -> None:
    config = migration_config(upgrade_database)
    command.upgrade(config, "0046_issue166_retire_ai_history")
    with Session(upgrade_database) as session, session.begin():
        adapter = Adapter(name="Existing task", language="python", adapter_type="task")
        session.add(adapter)
        session.flush()
        version = AdapterVersion(
            adapter_id=adapter.id,
            seq=1,
            code="def handle(context, input):\n    return input\n",
            requirements="",
            runtime_config={"purpose": "ordinary-task"},
        )
        session.add(version)
        session.flush()
        adapter.latest_version_id = version.id
        session.add(
            AdapterInputConfig(
                adapter_id=adapter.id,
                source_type="json",
                json_value={"business_id": "existing", "nested": [None, False, 42]},
                revision=7,
            )
        )
        adapter_id, version_id = adapter.id, version.id

    command.upgrade(config, "head")

    with Session(upgrade_database) as session:
        saved = session.get(Adapter, adapter_id)
        assert saved is not None and saved.latest_version_id == version_id
        assert saved.run_mode == "manual"
        revision = session.get(AdapterVersion, version_id)
        assert revision is not None
        assert revision.code == "def handle(context, input):\n    return input\n"
        assert revision.runtime_config == {"purpose": "ordinary-task"}
        current = session.get(AdapterInputConfig, adapter_id)
        assert current is not None and current.revision == 7
        assert current.json_value == {"business_id": "existing", "nested": [None, False, 42]}
        assert session.scalar(text("SELECT count(*) FROM event_subscriptions")) == 0
        assert session.scalar(text("SELECT count(*) FROM event_receipts")) == 0
        constraint = session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='executions'::regclass AND conname='ck_executions_trigger'"
            )
        )
        assert constraint is not None
        assert all(
            trigger in constraint for trigger in ("manual", "schedule", "webhook", "subscription")
        )


def test_fresh_schema_installs_subscription_tables_without_default_objects(
    upgrade_database: Engine,
) -> None:
    command.upgrade(migration_config(upgrade_database), "head")
    with upgrade_database.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM event_subscriptions")) == 0
        assert connection.scalar(text("SELECT count(*) FROM event_deliveries")) == 0
        assert connection.scalar(text("SELECT count(*) FROM adapters")) == 0
