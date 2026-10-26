"""Real PostgreSQL transactions for the exact opt-in business-batch example."""

import json
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from test_issue222_reference_adapter import Context, event, reference
from test_issue222_reference_adapter_batch import assert_valid, frame
from test_issue222_reference_adapter_database import prepare_target


def target_rows(factory: sessionmaker[Session], run_id: str) -> list[Any]:
    with factory() as session:
        return list(
            session.execute(
                text(
                    "SELECT message_id,seq,payload_sha256,normalized_value,writes,attempts "
                    "FROM dlr_ingress_results WHERE run_id=:run_id ORDER BY message_id"
                ),
                {"run_id": run_id},
            ).all()
        )


def test_real_batch_reuses_one_connection_but_commits_each_member_independently(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(prepare_target(session_factory))
    run_id = "db-batch-transactions-" + uuid4().hex
    source = frame(4, run_id)
    value = source["members"][1]["input"]["data"]["payload"]
    source["members"][1]["input"] = event(value, "rabbitmq")
    original_connect, original_transaction = reference._connect, reference._transaction
    connections, visible_prefixes = [], []

    async def connect(*args: Any) -> Any:
        connection = await original_connect(*args)
        connections.append(connection)
        return connection

    async def transaction(connection: Any, item: Any, *args: Any) -> Any:
        # A distinct PostgreSQL session sees the preceding effects before this
        # member starts; there is no hidden encompassing batch transaction.
        visible_prefixes.append(len(target_rows(session_factory, run_id)))
        return await original_transaction(connection, item, *args)

    monkeypatch.setattr(reference, "_connect", connect)
    monkeypatch.setattr(reference, "_transaction", transaction)
    result = reference.handle(context, source)
    assert_valid(source, result)
    assert all(item["status"] == "succeeded" for item in result["members"])
    assert visible_prefixes == [0, 1, 2, 3]
    assert len(connections) == 1 and connections[0].closed
    rows = target_rows(session_factory, run_id)
    assert [tuple(row[3:]) for row in rows] == [(3, 1, 1)] * 4
    assert [row[2] for row in rows] == [
        reference._payload(item["input"])[3] for item in source["members"]
    ]


def test_real_permission_failure_preserves_committed_prefix_and_never_opens_successor_transaction(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = prepare_target(session_factory)
    context, run_id = Context(dsn), "db-batch-denied-" + uuid4().hex
    source, role = frame(3, run_id), "batch_no_write_" + uuid4().hex
    original, attempted = reference._transaction, []
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))

    async def transaction(connection: Any, item: Any, *args: Any) -> Any:
        attempted.append(item[2])
        if item[2] == 2:
            await connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        return await original(connection, item, *args)

    monkeypatch.setattr(reference, "_transaction", transaction)
    try:
        result = reference.handle(context, source)
        assert_valid(source, result)
        assert attempted == [1, 2]
        assert [(item["status"], item["error_code"]) for item in result["members"]] == [
            ("succeeded", None),
            ("failed", "target_permission_denied"),
            ("not_run", "predecessor_failed"),
        ]
        rows = target_rows(session_factory, run_id)
        assert len(rows) == 1 and tuple(rows[0][3:]) == (3, 1, 1)
        failures = [
            json.loads(message)
            for message in context.messages
            if '"reference_batch_failure"' in message
        ]
        assert len(failures) == 1 and failures[0]["sqlstate"] == "42501"
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_real_commit_response_loss_recovers_only_current_member_without_rewriting_prefix(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(prepare_target(session_factory))
    run_id = "db-batch-commit-unknown-" + uuid4().hex
    source, original = frame(3, run_id), reference._transaction
    attempted = []

    async def transaction(connection: Any, item: Any, *args: Any) -> Any:
        result = await original(connection, item, *args)
        attempted.append(item[2])
        if item[2] == 2 and attempted.count(2) == 1:
            # The actual second member COMMIT has returned. Lose its client
            # result before the invocation can confirm it, then reconnect.
            raise psycopg.OperationalError("injected loss after actual member commit")
        return result

    monkeypatch.setattr(reference, "_transaction", transaction)
    monkeypatch.setattr(reference, "INITIAL_BACKOFF_SECONDS", 0.001)
    result = reference.handle(context, source)
    assert_valid(source, result)
    assert all(item["status"] == "succeeded" for item in result["members"])
    assert attempted == [1, 2, 2, 3]
    rows = target_rows(session_factory, run_id)
    assert [tuple(row[3:]) for row in rows] == [(3, 1, 1), (3, 1, 2), (3, 1, 1)]
    assert json.loads(context.messages[-1])["extra_retries_used"] == 1
