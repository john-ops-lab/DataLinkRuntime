"""Fresh-schema assertions for the current Alembic head."""

from sqlalchemy import text
from sqlalchemy.engine import Engine


def test_fresh_schema_has_task_run_mode_and_active_execution_contract(
    test_engine: Engine,
) -> None:
    with test_engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        columns = set(
            connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'adapters'"
                )
            ).all()
        )
        index_definition = connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = 'uq_executions_active_adapter'"
            )
        )
        adapter_type_check = connection.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_adapters_adapter_type'"
            )
        )
        webhook_index = connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname = 'uq_adapter_webhooks_enabled_public_id'"
            )
        )
        webhook_credential_nullable = connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'adapter_webhooks' "
                "AND column_name = 'credential_id'"
            )
        )
        # M5.5.11: the single-run execution timeout is a NOT NULL Adapter column.
        timeout_nullable = connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'adapters' "
                "AND column_name = 'timeout_seconds'"
            )
        )
        execution_locale_nullable = connection.scalar(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'executions' "
                "AND column_name = 'locale'"
            )
        )
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

    assert revision == "0031_issue130_b2_runtime"
    assert {
        "adapter_type",
        "run_mode",
        "timeout_seconds",
        "owner_user_id",
        "latest_version_id",
        "runtime_worker_id",
        "archived_at",
    } <= columns
    assert {
        "published_version_id",
        "production_version_id",
        "production_worker_id",
        "production_state",
    }.isdisjoint(columns)
    assert index_definition is not None
    assert "status" in index_definition
    assert "trigger" not in index_definition
    assert adapter_type_check is not None
    assert "task" in adapter_type_check and "webhook" in adapter_type_check
    assert webhook_index is not None
    assert "UNIQUE" in webhook_index and "WHERE enabled" in webhook_index
    assert webhook_credential_nullable == "YES"
    assert timeout_nullable == "NO"
    assert execution_locale_nullable == "NO"
    assert lease_created_at[0] == "NO"
    assert lease_created_at[1] is not None and "now()" in lease_created_at[1]
    assert cleanup_status_check is not None and "pending" in cleanup_status_check
