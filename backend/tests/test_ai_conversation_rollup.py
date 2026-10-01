"""#151 rollup CAS against isolated PostgreSQL and a fake Provider."""

import json
import os
import time
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.ai import conversation_rollup, conversation_summary, providers
from dlr.control.ai.conversation_contract import ConversationState, SummarySnapshot
from dlr.control.schemas.ai import AiSettingDraft

DATABASE = "dlr_test_issue151_rollup"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    base = make_url(settings.database_url).set(database="postgres")
    maintenance = create_engine(base, isolation_level="AUTOCOMMIT")
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {DATABASE}"))
    url = base.set(database=DATABASE)
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    original = os.environ.pop("DATABASE_URL", None)
    try:
        command.upgrade(config, "head")
    finally:
        if original is not None:
            os.environ["DATABASE_URL"] = original
    result = create_engine(url)
    yield result
    result.dispose()
    with maintenance.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)"))
    maintenance.dispose()


def _seed(engine: Engine, *, gap: bool = False) -> uuid.UUID:
    conversation_id = uuid.uuid4()
    with engine.begin() as connection:
        adapter = connection.scalar(
            text(
                "INSERT INTO adapters (name, language, adapter_type) "
                "VALUES (:name, 'python', 'task') RETURNING id"
            ),
            {"name": f"rollup-{conversation_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO ai_conversations "
                "(id, adapter_id, owner_kind, next_sequence, expires_at) "
                "VALUES (:id, :adapter, 'deployment', 5, now() + interval '1 day')"
            ),
            {"id": conversation_id, "adapter": adapter},
        )
        rows = (
            (1, "user", "Use Python without new dependencies.", "completed"),
            (2, "assistant", "I will use Python.", None),
            (3, "user", "You may add dependencies now.", "completed"),
            (4, "assistant", "Understood.", None),
        )
        for sequence, role, content, status in rows:
            if gap and sequence == 2:
                continue
            connection.execute(
                text(
                    "INSERT INTO ai_conversation_messages "
                    "(id, conversation_id, sequence, turn_id, role, content, generation, "
                    "request_status, idempotency_key, request_hmac, response_meta_json) "
                    "VALUES (:id, :conversation, :sequence, :turn, :role, :content, 1, "
                    ":status, :key, :hmac, :meta)"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation": conversation_id,
                    "sequence": sequence,
                    "turn": uuid.UUID(int=(sequence + 1) // 2),
                    "role": role,
                    "content": content,
                    "status": status,
                    "key": uuid.uuid4() if role == "user" else None,
                    "hmac": bytes(32) if role == "user" else None,
                    "meta": json.dumps({}) if role == "assistant" else None,
                },
            )
    return conversation_id


def _draft() -> AiSettingDraft:
    return AiSettingDraft(
        provider="custom_openai_compatible",
        base_url="http://fake-provider.invalid",
        model="fake",
        credential_id=None,
        reasoning_mode="default",
        reasoning_effort=None,
    )


def _output(through: int) -> str:
    sources = [
        {"sequence": number, "revision": 1, "role": role}
        for number, role in ((1, "user"), (2, "assistant"), (3, "user"), (4, "assistant"))
    ]
    result: dict[str, object] = {
        "version": 1,
        "text": "The user's current preference is recorded.",
        "sources": [sources[0], sources[2]] if through == 4 else [sources[0]],
        "covered_through": through,
    }
    if through == 2:
        result["state_additions"] = [
            {
                "id": "constraint-1",
                "kind": "explicit_constraint",
                "confirmation_level": "explicit",
                "text": "No new dependencies",
                "source": sources[0],
                "evidence_quote": "without new dependencies",
            }
        ]
    else:
        result["state_revocations"] = [
            {
                "fact_id": "constraint-1",
                "source": sources[2],
                "evidence_quote": "You may add dependencies now",
            }
        ]
    return json.dumps(result)


def _rollup(
    engine: Engine, conversation_id: uuid.UUID, recent_from: int
) -> conversation_rollup.RollupResult:
    return conversation_rollup.rollup_before_recent_window(
        lambda: Session(engine),
        conversation_id,
        recent_from_sequence=recent_from,
        draft=_draft(),
        api_key=None,
        adapter=providers.get_provider("custom_openai_compatible"),
        hard_deadline=time.monotonic() + 30,
        call_budget=conversation_summary.SummaryCallBudget(),
    )


def test_incremental_summary_and_sourced_revocation(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = _seed(engine)
    calls: list[int] = []

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        messages = args[2]
        assert isinstance(messages, list)
        request = json.loads(str(messages[1]["content"]))
        through = int(request["coverage_through"])
        calls.append(through)
        return _output(through), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    assert _rollup(engine, conversation_id, 3).accepted
    with Session(engine) as session:
        row = session.execute(
            text("SELECT state_json, summary_source_revisions FROM ai_conversations WHERE id=:id"),
            {"id": conversation_id},
        ).one()
        assert (
            len(ConversationState.model_validate_json(json.dumps(row.state_json)).active_facts())
            == 1
        )
        assert len(row.summary_source_revisions) == 2
        assert all("role" in item for item in row.summary_source_revisions)
    assert _rollup(engine, conversation_id, 5).accepted
    with Session(engine) as session:
        row = session.execute(
            text("SELECT state_json, summary_covered_through FROM ai_conversations WHERE id=:id"),
            {"id": conversation_id},
        ).one()
        state = ConversationState.model_validate_json(json.dumps(row.state_json))
        assert row.summary_covered_through == 4
        assert state.revision == 2
        assert state.active_facts() == ()
        assert state.facts[0].revoked_by is not None
        assert state.facts[0].revoked_by.sequence == 3
    assert calls == [2, 4]


def test_changed_uncited_reply_forces_full_rebuild(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = _seed(engine)
    monkeypatch.setattr(providers, "chat_assist", lambda *args, **kwargs: (_output(2), None))
    assert _rollup(engine, conversation_id, 3).accepted
    with Session(engine) as session:
        assert conversation_rollup.invalidate_changed_reply(
            session, conversation_id, 2, new_revision=2
        )
        session.execute(
            text(
                "UPDATE ai_conversation_messages SET content='Updated reply', "
                "source_revision=2 WHERE conversation_id=:id AND sequence=2"
            ),
            {"id": conversation_id},
        )
        session.commit()
    with Session(engine) as session:
        plan, error = conversation_rollup.prepare_rollup(
            session, conversation_id, recent_from_sequence=3
        )
        assert error is None and plan is not None
        assert plan.previous is None
        assert [item.source.sequence for item in plan.pending] == [1, 2]
        assert plan.pending[1].source.revision == 2
        assert plan.state.active_facts() == ()


def test_failed_attempt_stale_cas_and_missing_original_do_not_advance(engine: Engine) -> None:
    conversation_id = _seed(engine)
    with Session(engine) as session:
        plan, error = conversation_rollup.prepare_rollup(
            session, conversation_id, recent_from_sequence=3
        )
    assert error is None and plan is not None
    with Session(engine) as session:
        failed = conversation_rollup.persist_rollup(
            session, plan, conversation_summary.SummaryAttempt(False, None, "ai_timeout")
        )
        assert failed.error_code == "ai_timeout"
        session.commit()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE ai_conversations SET revision=revision+1 WHERE id=:id"),
            {"id": conversation_id},
        )
    payload = json.loads(_output(2))
    payload.pop("state_additions")
    snapshot = SummarySnapshot.model_validate_json(json.dumps(payload))
    with Session(engine) as session:
        result = conversation_rollup.persist_rollup(
            session, plan, conversation_summary.SummaryAttempt(True, snapshot)
        )
        assert result.error_code == "ai_summary_stale"
        session.commit()
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT summary_json, summary_covered_through FROM ai_conversations WHERE id=:id"),
            {"id": conversation_id},
        ).one()
        assert row.summary_json is None and row.summary_covered_through == 0
    gap_id = _seed(engine, gap=True)
    with Session(engine) as session:
        _, error = conversation_rollup.prepare_rollup(session, gap_id, recent_from_sequence=5)
        assert error == "ai_summary_original_missing"


def test_invalid_evidence_and_provider_timeout_keep_valid_old_summary(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = _seed(engine)
    monkeypatch.setattr(providers, "chat_assist", lambda *args, **kwargs: (_output(2), None))
    assert _rollup(engine, conversation_id, 3).accepted
    with Session(engine) as session:
        plan, error = conversation_rollup.prepare_rollup(
            session, conversation_id, recent_from_sequence=5
        )
    assert error is None and plan is not None and plan.previous is not None
    bad = json.loads(_output(4))
    bad["state_revocations"][0]["evidence_quote"] = "a sentence never said"
    output = conversation_summary.SummaryOutput.model_validate_json(json.dumps(bad))
    snapshot = SummarySnapshot.model_validate(
        output.model_dump(exclude={"state_additions", "state_revocations"})
    )
    with Session(engine) as session:
        result = conversation_rollup.persist_rollup(
            session,
            plan,
            conversation_summary.SummaryAttempt(
                True, snapshot, revocations=output.state_revocations
            ),
        )
        assert result.error_code == "ai_summary_state_invalid"
        session.commit()

    def timeout(*args: object, **kwargs: object) -> tuple[None, None]:
        raise providers.AiProviderError("ai_timeout")

    monkeypatch.setattr(providers, "chat_assist", timeout)
    assert _rollup(engine, conversation_id, 5).error_code == "ai_summary_invalid"
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT summary_valid, summary_covered_through, state_json "
                "FROM ai_conversations WHERE id=:id"
            ),
            {"id": conversation_id},
        ).one()
        assert row.summary_valid and row.summary_covered_through == 2
        assert (
            len(ConversationState.model_validate_json(json.dumps(row.state_json)).active_facts())
            == 1
        )


def test_invalid_assistant_proposal_cannot_block_valid_user_constraint(engine: Engine) -> None:
    conversation_id = _seed(engine)
    with Session(engine) as session:
        plan, error = conversation_rollup.prepare_rollup(
            session, conversation_id, recent_from_sequence=3
        )
    assert error is None and plan is not None
    payload = json.loads(_output(2))
    payload["state_additions"].extend(
        [
            {
                "id": "false-decision",
                "kind": "confirmed_decision",
                "confirmation_level": "confirmed",
                "text": "Assistant statement falsely promoted to a decision",
                "source": {"sequence": 2, "revision": 1, "role": "assistant"},
                "evidence_quote": "I will use Python.",
            },
            {
                "id": "invented-quote",
                "kind": "inference",
                "confirmation_level": "inferred",
                "text": "Unquoted assistant inference",
                "source": {"sequence": 2, "revision": 1, "role": "assistant"},
                "evidence_quote": "not in the assistant message",
            },
        ]
    )
    output = conversation_summary.SummaryOutput.model_validate_json(json.dumps(payload))
    snapshot = SummarySnapshot.model_validate(
        output.model_dump(exclude={"state_additions", "state_revocations"})
    )
    with Session(engine) as session:
        result = conversation_rollup.persist_rollup(
            session,
            plan,
            conversation_summary.SummaryAttempt(True, snapshot, additions=output.state_additions),
        )
        assert result.accepted and result.covered_through == 2
        session.commit()
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT state_json FROM ai_conversations WHERE id=:id"),
            {"id": conversation_id},
        ).one()
    state = ConversationState.model_validate_json(json.dumps(row.state_json))
    assert [fact.id for fact in state.facts] == ["constraint-1"]


def test_uncited_source_revision_change_rejects_stale_plan(engine: Engine) -> None:
    conversation_id = _seed(engine)
    with Session(engine) as session:
        plan, error = conversation_rollup.prepare_rollup(
            session, conversation_id, recent_from_sequence=3
        )
    assert error is None and plan is not None
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ai_conversation_messages SET content='Different reply', "
                "source_revision=2 WHERE conversation_id=:id AND sequence=2"
            ),
            {"id": conversation_id},
        )
    payload = json.loads(_output(2))
    payload.pop("state_additions")
    snapshot = SummarySnapshot.model_validate_json(json.dumps(payload))
    with Session(engine) as session:
        result = conversation_rollup.persist_rollup(
            session, plan, conversation_summary.SummaryAttempt(True, snapshot)
        )
        assert result.error_code == "ai_summary_source_changed"
        session.commit()


def test_large_backlog_advances_in_bounded_contiguous_turns(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = _seed(engine)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE ai_conversations SET next_sequence=13 WHERE id=:id"),
            {"id": conversation_id},
        )
        for sequence in range(5, 13):
            role = "user" if sequence % 2 else "assistant"
            connection.execute(
                text(
                    "INSERT INTO ai_conversation_messages "
                    "(id, conversation_id, sequence, turn_id, role, content, generation, "
                    "request_status, idempotency_key, request_hmac, response_meta_json) "
                    "VALUES (:id, :conversation, :sequence, :turn, :role, :content, 1, "
                    ":status, :key, :hmac, :meta)"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation": conversation_id,
                    "sequence": sequence,
                    "turn": uuid.UUID(int=(sequence + 1) // 2),
                    "role": role,
                    "content": f"Visible message {sequence}",
                    "status": "completed" if role == "user" else None,
                    "key": uuid.uuid4() if role == "user" else None,
                    "hmac": bytes(32) if role == "user" else None,
                    "meta": json.dumps({}) if role == "assistant" else None,
                },
            )
    requests: list[tuple[int, list[int]]] = []

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        messages = args[2]
        assert isinstance(messages, list)
        request = json.loads(str(messages[1]["content"]))
        pending = request["pending_messages"]
        through = request["coverage_through"]
        requests.append((through, [item["source"]["sequence"] for item in pending]))
        return json.dumps(
            {
                "version": 1,
                "text": "Bounded summary",
                "sources": [pending[0]["source"]],
                "covered_through": through,
            }
        ), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    assert _rollup(engine, conversation_id, 13).covered_through == 8
    assert _rollup(engine, conversation_id, 13).covered_through == 12
    assert requests == [(8, list(range(1, 9))), (12, list(range(9, 13)))]


def test_early_constraint_and_revoke_in_one_pending_batch(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = _seed(engine)
    output = json.loads(_output(4))
    output["state_additions"] = json.loads(_output(2))["state_additions"]
    monkeypatch.setattr(
        providers, "chat_assist", lambda *args, **kwargs: (json.dumps(output), None)
    )
    assert _rollup(engine, conversation_id, 5).accepted
    with engine.connect() as connection:
        payload = connection.scalar(
            text("SELECT state_json FROM ai_conversations WHERE id=:id"),
            {"id": conversation_id},
        )
    state = ConversationState.model_validate_json(json.dumps(payload))
    assert state.revision == 2
    assert len(state.facts) == 1 and state.active_facts() == ()
    assert state.facts[0].introduced_revision == 1
    assert state.facts[0].revoked_revision == 2
