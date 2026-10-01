"""#151 additive 0044 -> 0045 migration against isolated PostgreSQL."""

import json
import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import IntegrityError

from dlr.common.config import settings
from dlr.control.ai.conversation_contract import ConversationState

MIGRATION_DATABASE = "dlr_test_issue151_ai_conversations"
OLD_REVISION = "0044_issue161_cache_admin"
NEW_REVISION = "0045_issue151_ai_conversations"


def _base_url() -> URL:
    return make_url(settings.database_url).set(database=None)


def _config(url: URL) -> Config:
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    return config


def _upgrade(config: Config, revision: str) -> None:
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(config, revision)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


def _downgrade(config: Config, revision: str) -> None:
    saved = os.environ.pop("DATABASE_URL", None)
    try:
        command.downgrade(config, revision)
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


@pytest.fixture()
def old_engine() -> Iterator[Engine]:
    url = _base_url().set(database=MIGRATION_DATABASE)
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    maintenance.dispose()
    _upgrade(_config(url), OLD_REVISION)
    engine = create_engine(url)
    yield engine
    engine.dispose()
    maintenance = create_engine(_base_url().set(database="postgres"), isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def _old_hashes(engine: Engine, adapter_id: int, user_id: int) -> tuple[str, str, str]:
    with engine.connect() as connection:
        adapter_hash = connection.scalar(
            text("SELECT md5(row_to_json(t)::text) FROM (SELECT * FROM adapters WHERE id=:id) t"),
            {"id": adapter_id},
        )
        version_hash = connection.scalar(
            text(
                "SELECT md5(row_to_json(t)::text) FROM "
                "(SELECT * FROM adapter_versions WHERE adapter_id=:id) t"
            ),
            {"id": adapter_id},
        )
        user_hash = connection.scalar(
            text("SELECT md5(row_to_json(t)::text) FROM (SELECT * FROM users WHERE id=:id) t"),
            {"id": user_id},
        )
    assert isinstance(adapter_hash, str)
    assert isinstance(version_hash, str)
    assert isinstance(user_hash, str)
    return adapter_hash, version_hash, user_hash


def _reject(engine: Engine, sql: str, params: dict[str, object]) -> None:
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(sql), params)


def test_0044_upgrade_preserves_old_rows_and_creates_bounded_owner_storage(
    old_engine: Engine,
) -> None:
    config = _config(old_engine.url)
    assert ScriptDirectory.from_config(config).get_current_head() == NEW_REVISION
    with old_engine.begin() as connection:
        adapter_id = connection.scalar(
            text(
                "INSERT INTO adapters (name, language, adapter_type) "
                "VALUES ('pre-ai-conversation', 'python', 'task') RETURNING id"
            )
        )
        user_id = connection.scalar(
            text(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES ('pre-ai-owner', 'old-hash', 'admin') RETURNING id"
            )
        )
        connection.execute(
            text(
                "INSERT INTO adapter_versions (adapter_id, seq, code) "
                "VALUES (:id, 1, 'def handle(context, input): return input')"
            ),
            {"id": adapter_id},
        )
    assert isinstance(adapter_id, int) and isinstance(user_id, int)
    old_hashes = _old_hashes(old_engine, adapter_id, user_id)
    old_columns = {
        table: {item["name"] for item in inspect(old_engine).get_columns(table)}
        for table in ("adapters", "adapter_versions", "users")
    }
    with old_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == OLD_REVISION

    _upgrade(config, "head")

    with old_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW_REVISION
    assert _old_hashes(old_engine, adapter_id, user_id) == old_hashes
    assert all(
        {item["name"] for item in inspect(old_engine).get_columns(table)} == old_columns[table]
        for table in old_columns
    )
    tables = set(inspect(old_engine).get_table_names())
    assert {"ai_conversations", "ai_conversation_messages"}.issubset(tables)
    conversation_columns = {
        item["name"] for item in inspect(old_engine).get_columns("ai_conversations")
    }
    assert {
        "owner_kind",
        "account_user_id",
        "adapter_id",
        "revision",
        "state_json",
        "summary_json",
        "summary_valid",
        "summary_covered_from",
        "summary_covered_through",
        "summary_source_revisions",
        "next_sequence",
        "expires_at",
    }.issubset(conversation_columns)
    message_columns = {
        item["name"] for item in inspect(old_engine).get_columns("ai_conversation_messages")
    }
    assert {
        "turn_id",
        "generation",
        "idempotency_key",
        "request_hmac",
        "request_status",
        "source_revision",
        "candidate_json",
        "response_meta_json",
        "base_version_id",
    }.issubset(message_columns)
    checks = {
        table: {item["name"] for item in inspect(old_engine).get_check_constraints(table)}
        for table in ("ai_conversations", "ai_conversation_messages")
    }
    assert {
        "ck_ai_conversations_owner",
        "ck_ai_conversations_expiry",
        "ck_ai_conversations_state_bound",
        "ck_ai_conversations_summary_bound",
        "ck_ai_conversations_sources_bound",
        "ck_ai_conversations_summary_range",
    }.issubset(checks["ai_conversations"])
    assert {
        "ck_ai_messages_role_payload",
        "ck_ai_messages_content_bound",
        "ck_ai_messages_candidate_bound",
    }.issubset(checks["ai_conversation_messages"])
    conversation_indexes = {
        item["name"] for item in inspect(old_engine).get_indexes("ai_conversations")
    }
    message_indexes = {
        item["name"] for item in inspect(old_engine).get_indexes("ai_conversation_messages")
    }
    assert {
        "ix_ai_conversations_account_list",
        "ix_ai_conversations_deployment_list",
        "ix_ai_conversations_expiry",
    }.issubset(conversation_indexes)
    assert "uq_ai_messages_current_key" in message_indexes

    account_session = uuid.uuid4()
    deployment_session = uuid.uuid4()
    with old_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ai_conversations "
                "(id, adapter_id, owner_kind, account_user_id, expires_at) "
                "VALUES (:id, :adapter, 'account', :user, now() + INTERVAL '30 days')"
            ),
            {"id": account_session, "adapter": adapter_id, "user": user_id},
        )
        connection.execute(
            text(
                "INSERT INTO ai_conversations "
                "(id, adapter_id, owner_kind, expires_at) "
                "VALUES (:id, :adapter, 'deployment', now() + INTERVAL '30 days')"
            ),
            {"id": deployment_session, "adapter": adapter_id},
        )
        state = connection.scalar(
            text("SELECT state_json FROM ai_conversations WHERE id=:id"),
            {"id": account_session},
        )
    assert ConversationState.model_validate_json(
        json.dumps(state), strict=True
    ) == ConversationState(revision=0)

    _reject(
        old_engine,
        "INSERT INTO ai_conversations (id, adapter_id, owner_kind, expires_at) "
        "VALUES (:id, :adapter, 'account', now() + INTERVAL '30 days')",
        {"id": uuid.uuid4(), "adapter": adapter_id},
    )
    _reject(
        old_engine,
        "INSERT INTO ai_conversations "
        "(id, adapter_id, owner_kind, account_user_id, expires_at) "
        "VALUES (:id, :adapter, 'deployment', :user, now() + INTERVAL '30 days')",
        {"id": uuid.uuid4(), "adapter": adapter_id, "user": user_id},
    )
    _reject(
        old_engine,
        "INSERT INTO ai_conversations (id, adapter_id, owner_kind, expires_at) "
        "VALUES (:id, :adapter, 'deployment', now() + INTERVAL '91 days')",
        {"id": uuid.uuid4(), "adapter": adapter_id},
    )
    _reject(
        old_engine,
        "UPDATE ai_conversations SET state_json = CAST(:state AS jsonb) WHERE id=:id",
        {"id": account_session, "state": json.dumps({"raw_attachment": "bad"})},
    )

    turn = uuid.uuid4()
    key = uuid.uuid4()
    user_message_id = uuid.uuid4()
    assistant_message_id = uuid.uuid4()
    with old_engine.begin() as connection:
        connection.execute(
            text("UPDATE ai_conversations SET next_sequence=3 WHERE id=:id"),
            {"id": account_session},
        )
        connection.execute(
            text(
                "INSERT INTO ai_conversation_messages "
                "(id, conversation_id, sequence, turn_id, role, content, generation, "
                "request_status, idempotency_key, request_hmac) "
                "VALUES (:id, :conversation, 1, :turn, 'user', 'write code', 1, "
                "'pending', :key, :hmac)"
            ),
            {
                "id": user_message_id,
                "conversation": account_session,
                "turn": turn,
                "key": key,
                "hmac": bytes(range(32)),
            },
        )
        connection.execute(
            text(
                "INSERT INTO ai_conversation_messages "
                "(id, conversation_id, sequence, turn_id, role, content, generation, "
                "response_meta_json, candidate_json) "
                "VALUES (:id, :conversation, 2, :turn, 'assistant', 'draft ready', 1, "
                '\'{"provider":"openai","model":"test","tool_calls":[]}\'::jsonb, '
                '\'{"summary":"draft","code":"return 1","required_secret_keys":[]}\'::jsonb)'
            ),
            {"id": assistant_message_id, "conversation": account_session, "turn": turn},
        )
        connection.execute(
            text("UPDATE ai_conversation_messages SET request_status='completed' WHERE id=:id"),
            {"id": user_message_id},
        )
    with old_engine.connect() as connection:
        replay = connection.execute(
            text(
                "SELECT u.generation AS requested_generation, u.request_status, "
                "a.generation AS reply_generation, a.content, a.candidate_json, "
                "a.response_meta_json FROM ai_conversation_messages u "
                "JOIN ai_conversation_messages a ON a.conversation_id=u.conversation_id "
                "AND a.turn_id=u.turn_id AND a.role='assistant' "
                "WHERE u.id=:id AND u.role='user' AND u.idempotency_key=:key"
            ),
            {"id": user_message_id, "key": key},
        ).one()
    assert replay.requested_generation == replay.reply_generation == 1
    assert replay.request_status == "completed"
    assert replay.content == "draft ready"
    assert replay.candidate_json["code"] == "return 1"
    assert replay.response_meta_json["provider"] == "openai"
    _reject(
        old_engine,
        "INSERT INTO ai_conversation_messages "
        "(id, conversation_id, sequence, turn_id, role, content, generation, "
        "request_status, idempotency_key, request_hmac) "
        "VALUES (:id, :conversation, 3, :turn, 'user', 'duplicate', 1, "
        "'pending', :key, :hmac)",
        {
            "id": uuid.uuid4(),
            "conversation": account_session,
            "turn": turn,
            "key": uuid.uuid4(),
            "hmac": bytes(range(32)),
        },
    )
    _reject(
        old_engine,
        "INSERT INTO ai_conversation_messages "
        "(id, conversation_id, sequence, turn_id, role, content, generation, "
        "request_status, idempotency_key, request_hmac) "
        "VALUES (:id, :conversation, 3, :turn, 'user', 'reuse key', 1, "
        "'pending', :key, :hmac)",
        {
            "id": uuid.uuid4(),
            "conversation": account_session,
            "turn": uuid.uuid4(),
            "key": key,
            "hmac": bytes(range(32)),
        },
    )
    _reject(
        old_engine,
        "UPDATE ai_conversation_messages SET content=:content "
        "WHERE conversation_id=:conversation AND role='assistant'",
        {"content": "x" * 16385, "conversation": account_session},
    )
    _reject(
        old_engine,
        "UPDATE ai_conversation_messages SET request_hmac=:hmac "
        "WHERE conversation_id=:conversation AND role='user'",
        {"hmac": bytes(range(31)), "conversation": account_session},
    )
    _reject(
        old_engine,
        "UPDATE ai_conversation_messages SET candidate_json = CAST(:candidate AS jsonb) "
        "WHERE conversation_id=:conversation AND role='assistant'",
        {
            "conversation": account_session,
            "candidate": json.dumps({"raw_provider_response": True}),
        },
    )
    _reject(
        old_engine,
        "UPDATE ai_conversation_messages SET candidate_json = CAST(:candidate AS jsonb) "
        "WHERE conversation_id=:conversation AND role='assistant'",
        {
            "conversation": account_session,
            "candidate": json.dumps({"summary": "bounded", "code": "x" * 131073}),
        },
    )
    _reject(
        old_engine,
        "UPDATE ai_conversation_messages SET response_meta_json = CAST(:response AS jsonb) "
        "WHERE conversation_id=:conversation AND role='assistant'",
        {
            "conversation": account_session,
            "response": json.dumps({"provider": "openai", "raw_provider_response": "bad"}),
        },
    )
    _reject(
        old_engine,
        "UPDATE ai_conversations SET summary_json = CAST(:summary AS jsonb), "
        "summary_valid=true, summary_covered_from=1, summary_covered_through=2, "
        "summary_source_revisions='[]'::jsonb WHERE id=:id",
        {
            "id": account_session,
            "summary": json.dumps(
                {"version": 1, "text": "summary", "sources": [], "covered_through": 2}
            ),
        },
    )

    source_versions = [
        {"sequence": 1, "revision": 1},
        {"sequence": 2, "revision": 1},
    ]
    with old_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ai_conversations SET summary_json=CAST(:summary AS jsonb), "
                "summary_valid=true, summary_covered_from=1, summary_covered_through=2, "
                "summary_source_revisions=CAST(:versions AS jsonb) WHERE id=:id"
            ),
            {
                "id": account_session,
                "summary": json.dumps(
                    {
                        "version": 1,
                        "text": "bounded",
                        "sources": [{"sequence": 1, "revision": 1, "role": "user"}],
                        "covered_through": 2,
                    }
                ),
                "versions": json.dumps(source_versions),
            },
        )
        assert (
            connection.scalar(
                text("SELECT summary_valid FROM ai_conversations WHERE id=:id"),
                {"id": account_session},
            )
            is True
        )

    # The current user row holds only one active generation/key. A late retry
    # using the superseded key cannot pass the generation/key CAS predicate;
    # the assistant row still contains the last successful reply meanwhile.
    next_key = uuid.uuid4()
    with old_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ai_conversation_messages SET generation=2, "
                "idempotency_key=:next_key, request_status='pending' WHERE id=:id"
            ),
            {"id": user_message_id, "next_key": next_key},
        )
        stale = connection.execute(
            text(
                "UPDATE ai_conversation_messages SET request_status='completed' "
                "WHERE id=:id AND generation=1 AND idempotency_key=:old_key"
            ),
            {"id": user_message_id, "old_key": key},
        )
        assert stale.rowcount == 0
        prior = connection.execute(
            text(
                "SELECT content, generation, source_revision, candidate_json "
                "FROM ai_conversation_messages WHERE id=:id"
            ),
            {"id": assistant_message_id},
        ).one()
        assert prior.content == "draft ready"
        assert prior.generation == prior.source_revision == 1
        assert prior.candidate_json["code"] == "return 1"

    with old_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ai_conversations WHERE id=:id"), {"id": account_session}
        )
    with old_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM ai_conversation_messages WHERE conversation_id=:id"),
                {"id": account_session},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM adapters WHERE id=:id"), {"id": adapter_id}
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM ai_conversations WHERE id=:id"),
                {"id": deployment_session},
            )
            == 1
        )

    account_session_after_clear = uuid.uuid4()
    with old_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ai_conversations "
                "(id, adapter_id, owner_kind, account_user_id, expires_at) "
                "VALUES (:id, :adapter, 'account', :user, now() + INTERVAL '30 days')"
            ),
            {"id": account_session_after_clear, "adapter": adapter_id, "user": user_id},
        )
        connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": user_id})
    with old_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM ai_conversations WHERE id=:id"),
                {"id": account_session_after_clear},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT count(*) FROM ai_conversations WHERE id=:id"),
                {"id": deployment_session},
            )
            == 1
        )

    with pytest.raises(RuntimeError, match="AI conversations exist"):
        _downgrade(config, OLD_REVISION)
    with old_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == NEW_REVISION
    with old_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ai_conversations WHERE id=:id"), {"id": deployment_session}
        )
    _downgrade(config, OLD_REVISION)
    assert "ai_conversations" not in set(inspect(old_engine).get_table_names())
    with old_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == OLD_REVISION
        assert (
            connection.scalar(
                text("SELECT count(*) FROM adapters WHERE id=:id"), {"id": adapter_id}
            )
            == 1
        )
