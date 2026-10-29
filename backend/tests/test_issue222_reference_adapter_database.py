"""Real PostgreSQL idempotency, unknown commit outcome and permission behavior."""

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from test_issue222_reference_adapter import Context, body, event, reference


def prepare_target(factory: sessionmaker[Session]) -> str:
    schema = Path(__file__).resolve().parents[2] / "tools/ingress/target.sql"
    with factory.begin() as session:
        for statement in schema.read_text().split(";"):
            if statement.strip():
                session.execute(text(statement))
        connection = session.connection().connection.driver_connection
        assert isinstance(connection, psycopg.Connection)
        # Resolve outside the bounded invocation, using the endpoint already
        # reached by this fixture; retain the original host for TLS identity.
        hostaddr = connection.info.hostaddr
    engine = factory.kw["bind"]
    return (
        engine.url.set(drivername="postgresql")
        .update_query_dict({"hostaddr": hostaddr})
        .render_as_string(hide_password=False)
    )


def test_real_target_same_key_same_content_reads_original_and_conflict_preserves_it(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    context = Context(prepare_target(session_factory))
    item = body(run_id="db-selftest-" + uuid4().hex)
    first = reference.handle(context, event(item))
    second = reference.handle(context, event(item, "rabbitmq"))
    assert first["target_outcome"] == "inserted"
    assert second["target_outcome"] == "duplicate"
    assert first["target_attempt_count"] == second["target_attempt_count"] == 1
    assert first["payload_sha256"] == second["payload_sha256"]
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert first["payload_sha256"] == hashlib.sha256(canonical).hexdigest()
    with pytest.raises(RuntimeError, match="reference_target_write_failed"):
        reference.handle(context, event({**item, "value": 9}))
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT seq,payload_sha256,normalized_value,writes,attempts "
                "FROM dlr_ingress_results WHERE run_id=:run_id"
            ),
            {"run_id": item["run_id"]},
        ).one()
        assert tuple(row) == (1, first["payload_sha256"], 3, 1, 2)
        assert (
            session.scalar(
                text("SELECT count(*) FROM dlr_ingress_attempts WHERE run_id=:run_id"),
                {"run_id": item["run_id"]},
            )
            == 2
        )


def test_real_commit_then_client_result_loss_recovers_original_effect(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(prepare_target(session_factory))
    item = body(run_id="db-commit-unknown-" + uuid4().hex)
    original = reference._write_once
    committed = []

    async def lose_first_result(*args: Any) -> Any:
        result = await original(*args)
        committed.append(result)
        if len(committed) == 1:
            # Exact test fault: the real target transaction already committed,
            # then the client loses its result before the Adapter can use it.
            raise psycopg.OperationalError("injected loss after actual commit")
        return result

    monkeypatch.setattr(reference, "_write_once", lose_first_result)
    monkeypatch.setattr(reference, "INITIAL_BACKOFF_SECONDS", 0.001)
    result = reference.handle(context, event(item))
    assert result["target_attempt_count"] == 2
    assert result["target_outcome"] == "duplicate"
    assert committed[0]["target_outcome"] == "inserted"
    with session_factory() as session:
        row = session.execute(
            text("SELECT writes,attempts FROM dlr_ingress_results WHERE run_id=:run_id"),
            {"run_id": item["run_id"]},
        ).one()
        assert tuple(row) == (1, 2)


def test_real_target_permission_denial_is_not_retried(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    admin_dsn = prepare_target(session_factory)
    role = "reference_no_write_" + uuid4().hex
    password = uuid4().hex
    denied_dsn = make_conninfo(admin_dsn, user=role, password=password)
    with psycopg.connect(admin_dsn) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    try:
        context = Context(denied_dsn)
        with pytest.raises(RuntimeError, match="reference_target_write_failed"):
            reference.handle(context, event(body(run_id="db-denied-" + uuid4().hex)))
        failures = [json.loads(item) for item in context.messages]
        assert len([item for item in failures if item.get("attempt")]) == 1
        assert any(item["sqlstate"] == "42501" for item in failures)
        assert all(password not in item for item in context.messages)
    finally:
        with psycopg.connect(admin_dsn) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_real_target_wrong_password_is_not_blindly_retried(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    wrong_dsn = make_conninfo(
        prepare_target(session_factory), password="deliberately-wrong-reference-password"
    )
    context = Context(wrong_dsn)
    with pytest.raises(RuntimeError, match="reference_target_write_failed"):
        reference.handle(context, event(body(run_id="db-wrong-password-" + uuid4().hex)))
    failures = [json.loads(item) for item in context.messages]
    attempts = [item for item in failures if item.get("attempt")]
    assert len(attempts) == 1
    assert attempts[0]["error_type"] == "AuthenticationUnverified"
    assert all("deliberately-wrong" not in item for item in context.messages)
