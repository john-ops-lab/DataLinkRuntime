"""M5.4.3 migration compatibility for legacy M5.3 Webhook paths."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.schemas.webhook import WebhookUpsert
from dlr.control.services.webhook import upsert_webhook

MIGRATION_DATABASE = "dlr_test_migration_m5_4_3"
LEGACY_REVISION = "0010_m5_4_2_task_run_mode"
FINAL_REVISION = "0029_issue127_c0_exec_lease"
LEGACY_PUBLIC_ID = "Legacy_Path_ABC123"


def _base_url() -> URL:
    return make_url(settings.database_url).set(database=None)


def _alembic_config(url: URL) -> Config:
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    return config


def _upgrade(config: Config, revision: str) -> None:
    saved_database_url = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(config, revision)
    finally:
        if saved_database_url is not None:
            os.environ["DATABASE_URL"] = saved_database_url


@pytest.fixture()
def legacy_webhook_engine() -> Iterator[Engine]:
    """A dedicated database upgraded only to the pre-M5.4.3 revision."""
    url = _base_url().set(database=MIGRATION_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    maintenance.dispose()

    _upgrade(_alembic_config(url), LEGACY_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()

    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def test_legacy_path_survives_upgrade_and_remains_stoppable_and_restartable(
    legacy_webhook_engine: Engine,
) -> None:
    with legacy_webhook_engine.begin() as connection:
        worker_id = int(
            connection.scalar(
                text(
                    "INSERT INTO workers (name, status, last_heartbeat, capabilities) "
                    "VALUES ('legacy-webhook-worker', 'online', now(), '[\"python\"]'::jsonb) "
                    "RETURNING id"
                )
            )
        )
        adapter_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapters "
                    "(name, language, adapter_type, runtime_worker_id) "
                    "VALUES ('legacy-webhook', 'python', 'webhook', :worker_id) RETURNING id"
                ),
                {"worker_id": worker_id},
            )
        )
        version_id = int(
            connection.scalar(
                text(
                    "INSERT INTO adapter_versions (adapter_id, seq, code) "
                    "VALUES (:adapter_id, 1, 'def handle(context, input): return input') "
                    "RETURNING id"
                ),
                {"adapter_id": adapter_id},
            )
        )
        credential_id = int(
            connection.scalar(
                text(
                    "INSERT INTO credentials (name, type, ciphertext) "
                    "VALUES ('legacy-webhook-token', 'token', 'legacy-ciphertext') RETURNING id"
                )
            )
        )
        connection.execute(
            text("UPDATE adapters SET latest_version_id = :version_id WHERE id = :adapter_id"),
            {"version_id": version_id, "adapter_id": adapter_id},
        )
        connection.execute(
            text(
                "INSERT INTO adapter_webhooks "
                "(adapter_id, public_id, enabled, credential_id) "
                "VALUES (:adapter_id, :public_id, true, :credential_id)"
            ),
            {
                "adapter_id": adapter_id,
                "public_id": LEGACY_PUBLIC_ID,
                "credential_id": credential_id,
            },
        )

    _upgrade(_alembic_config(_base_url().set(database=MIGRATION_DATABASE)), FINAL_REVISION)

    with legacy_webhook_engine.connect() as connection:
        lease_created_at = connection.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND table_name = 'execution_input_artifact_leases' "
                "AND column_name = 'created_at'"
            )
        ).one()
        cleanup_status_check = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_executions_workspace_cleanup_status'"
            )
        )
    assert lease_created_at[0] == "NO"
    assert lease_created_at[1] is not None and "now()" in lease_created_at[1]
    assert cleanup_status_check is not None and "pending" in cleanup_status_check

    with Session(legacy_webhook_engine) as session:
        stopped = upsert_webhook(
            session,
            adapter_id,
            WebhookUpsert(
                enabled=False,
                public_id=LEGACY_PUBLIC_ID,
                credential_id=credential_id,
            ),
        )
        assert stopped.enabled is False
        assert stopped.public_id == LEGACY_PUBLIC_ID

        restarted = upsert_webhook(
            session,
            adapter_id,
            WebhookUpsert(
                enabled=True,
                public_id=LEGACY_PUBLIC_ID,
                credential_id=credential_id,
            ),
        )
        assert restarted.enabled is True
        assert restarted.hook_path == f"/api/hooks/{LEGACY_PUBLIC_ID}"

        upsert_webhook(
            session,
            adapter_id,
            WebhookUpsert(
                enabled=False,
                public_id=LEGACY_PUBLIC_ID,
                credential_id=credential_id,
            ),
        )
        with pytest.raises(HTTPException) as exc_info:
            upsert_webhook(
                session,
                adapter_id,
                WebhookUpsert(
                    enabled=False,
                    public_id="Another_Invalid_Path",
                    credential_id=credential_id,
                ),
            )
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "webhook_path_invalid"
        session.rollback()

        changed = upsert_webhook(
            session,
            adapter_id,
            WebhookUpsert(
                enabled=False,
                public_id="receive-legacy-data",
                credential_id=credential_id,
            ),
        )
        assert changed.public_id == "receive-legacy-data"
