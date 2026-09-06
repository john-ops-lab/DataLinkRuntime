"""Upgrading an existing stopped Webhook preserves its old response behavior."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_unified_runtime_migration import _isolated_schema, _upgrade


def test_existing_webhook_gets_accepted_policy_and_database_constraints():
    with _isolated_schema("webhook_response", "0033_unified_execution") as (engine, database):
        with engine.begin() as connection:
            adapter_id = connection.scalar(
                text(
                    "INSERT INTO adapters (name, language, adapter_type) "
                    "VALUES ('existing-webhook', 'python', 'webhook') RETURNING id"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO adapter_webhooks (adapter_id, public_id, enabled) "
                    "VALUES (:id, 'existing-webhook', false)"
                ),
                {"id": adapter_id},
            )
        _upgrade(database, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT response_mode, response_timeout_seconds, public_id "
                    "FROM adapter_webhooks"
                )
            ).one()
            assert tuple(row) == ("accepted", 30, "existing-webhook")
        for assignment in [
            "response_mode = 'unknown'",
            "response_timeout_seconds = 0",
            "response_timeout_seconds = 301",
        ]:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text(f"UPDATE adapter_webhooks SET {assignment}"))
