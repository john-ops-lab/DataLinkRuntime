"""Durable Assist retry, regeneration, and CAS using PostgreSQL/Fake Provider."""

import json
import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.ai import providers
from dlr.control.schemas.ai import AiAssistRequest, AiAssistResponse
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import ai_session_turn


def create_adapter(client: TestClient, name: str) -> dict[str, Any]:
    response = client.post(
        "/api/adapters", json={"name": name, "language": "python", "adapter_type": "task"}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def configure(client: TestClient) -> None:
    response = client.put(
        "/api/ai/settings",
        json={
            "provider": "custom_openai_compatible",
            "base_url": "http://fake-provider.invalid",
            "model": "manual-model-id",
            "credential_id": None,
            "reasoning_mode": "default",
            "reasoning_effort": None,
        },
    )
    assert response.status_code == 200, response.text


def assist_body(code: str) -> dict[str, object]:
    return {
        "message": "Explain this adapter and improve it.",
        "working_copy": {"code": code, "requirements": "", "runtime_config": {}},
        "recent_messages": [],
        "base_version_id": None,
    }


def valid_output() -> dict[str, Any]:
    return {
        "message": "Generated a candidate.",
        "candidate": {
            "summary": "Keep the adapter behavior",
            "code": "def handle(context, input): return input",
            "requirements": "",
            "runtime_config": {},
            "required_secret_keys": [],
        },
    }


def _session(api_client: TestClient, adapter_id: int) -> tuple[str, str]:
    path = f"/api/adapters/{adapter_id}/ai"
    response = api_client.post(f"{path}/sessions")
    assert response.status_code == 201, response.text
    return path, response.json()["id"]


def _request(
    session_id: str,
    *,
    turn_id: uuid.UUID | None = None,
    key: uuid.UUID | None = None,
    message: str | None = None,
    regenerate: bool = False,
    expected_generation: int | None = None,
    expected_session_revision: int = 0,
    code: str = "def handle(context, input): return input",
) -> dict[str, object]:
    turn = turn_id or uuid.uuid4()
    payload = assist_body(code)
    payload.update(
        {
            "session_id": session_id,
            "turn_id": str(turn),
            "idempotency_key": str(key or uuid.uuid4()),
            "expected_generation": (
                expected_generation if expected_generation is not None else 1 if regenerate else 0
            ),
            "expected_session_revision": expected_session_revision,
        }
    )
    if message is not None:
        payload["message"] = message
    if regenerate:
        payload["regenerate_turn_id"] = str(turn)
    return payload


def _revision(client: TestClient, path: str, session_id: str) -> int:
    response = client.get(f"{path}/sessions/{session_id}")
    assert response.status_code == 200, response.text
    return int(response.json()["revision"])


def _fake_success(
    monkeypatch: pytest.MonkeyPatch, *, message: str = "Saved answer"
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        messages = args[2]
        assert isinstance(messages, list)
        requests.append({"messages": messages, "tools": kwargs.get("tools")})
        output = valid_output()
        output["message"] = message
        return json.dumps(output), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    return requests


def test_same_key_replay_conflict_and_regeneration_use_one_user_row(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "durable-turn")
    adapter_id = int(adapter["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    requests = _fake_success(monkeypatch)
    first_body = _request(session_id)
    first = api_client.post(f"{path}/assist", json=first_body)
    assert first.status_code == 200, first.text
    assert first.json()["message"] == "Saved answer"
    assert len(requests) == 1
    # A same-key response-loss replay ignores the now-advanced session revision.
    assert _revision(api_client, path, session_id) > int(first_body["expected_session_revision"])
    replay = api_client.post(f"{path}/assist", json=first_body)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert len(requests) == 1

    conflict_body = {
        **first_body,
        "working_copy": {
            "code": "different current code",
            "requirements": "",
            "runtime_config": {},
        },
    }
    conflict = api_client.post(f"{path}/assist", json=conflict_body)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ai_session_key_conflict"
    assert len(requests) == 1

    regen_body = _request(
        session_id,
        expected_session_revision=_revision(api_client, path, session_id),
        turn_id=uuid.UUID(str(first_body["turn_id"])),
        regenerate=True,
        message=str(first_body["message"]),
        code="def handle(context, input): return 'current'",
    )
    regen = api_client.post(f"{path}/assist", json=regen_body)
    assert regen.status_code == 200, regen.text
    assert len(requests) == 2
    current_user = requests[-1]["messages"][-1]["content"]
    assert "return 'current'" in str(current_user)
    assert "return 'current'" not in str(requests[0]["messages"][-1]["content"])
    old_key = api_client.post(f"{path}/assist", json=first_body)
    assert old_key.status_code == 409
    assert old_key.json()["detail"]["code"] == "ai_session_stale_revision"
    old_key_spoof_regen = api_client.post(
        f"{path}/assist",
        json={
            **first_body,
            "regenerate_turn_id": first_body["turn_id"],
            "expected_session_revision": _revision(api_client, path, session_id),
        },
    )
    assert old_key_spoof_regen.status_code == 409
    assert old_key_spoof_regen.json()["detail"]["code"] == "ai_session_stale_generation"

    detail = api_client.get(f"{path}/sessions/{session_id}").json()
    assert [item["sequence"] for item in detail["messages"]] == [1, 2]
    assert detail["messages"][0]["generation"] == 2
    assert detail["messages"][1]["generation"] == 2
    assert detail["messages"][1]["source_revision"] == 2
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT sequence, role, candidate_json, response_meta_json "
                "FROM ai_conversation_messages WHERE conversation_id=:id ORDER BY sequence"
            ),
            {"id": uuid.UUID(session_id)},
        ).all()
        assert len(rows) == 2
        assert rows[0].candidate_json is None
        assert rows[1].candidate_json["code"] == valid_output()["candidate"]["code"]
        assert set(rows[1].response_meta_json) == {"provider", "model", "tool_calls"}


def test_failed_turn_continuation_creates_visible_placeholder_and_retry_replaces_it(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-failure")["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    first_body = _request(session_id, message="First turn")

    def fail(*args: object, **kwargs: object) -> tuple[None, None]:
        raise providers.AiProviderError("ai_timeout")

    monkeypatch.setattr(providers, "chat_assist", fail)
    first = api_client.post(f"{path}/assist", json=first_body)
    assert first.status_code != 200
    second_body = _request(
        session_id,
        message="Continue after failure",
        expected_session_revision=_revision(api_client, path, session_id),
    )
    requests = _fake_success(monkeypatch)
    second = api_client.post(f"{path}/assist", json=second_body)
    assert second.status_code == 200, second.text
    detail = api_client.get(f"{path}/sessions/{session_id}").json()
    assert [item["sequence"] for item in detail["messages"]] == [1, 2, 3, 4]
    assert "did not produce" in detail["messages"][1]["content"]
    assert detail["messages"][0]["request_status"] == "failed"
    assert len(requests) == 1
    assert any("did not produce" in str(message["content"]) for message in requests[0]["messages"])

    retried = api_client.post(f"{path}/assist", json=first_body)
    assert retried.status_code == 200, retried.text
    detail = api_client.get(f"{path}/sessions/{session_id}").json()
    assert [item["sequence"] for item in detail["messages"]] == [1, 2, 3, 4]
    assert detail["messages"][1]["content"] == "Saved answer"
    assert detail["messages"][1]["source_revision"] == 2


def test_late_completion_after_clear_and_new_generation_is_fenced(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-cas")["id"])
    path, session_id = _session(api_client, adapter_id)
    body = _request(session_id)
    payload = AiAssistRequest.model_validate(body)
    digest = ai_session_turn._request_hmac(payload)
    with session_factory() as session:
        reservation = ai_session_turn._reserve_turn(
            session, adapter_id, payload, SUPERADMIN_PRINCIPAL, digest
        )
        assert isinstance(reservation, ai_session_turn.TurnReservation)
        session.commit()
    cleared = api_client.post(f"{path}/sessions/{session_id}/clear")
    assert cleared.status_code == 200
    output = valid_output()
    response = AiAssistResponse.model_validate(
        {
            **output,
            "provider": "custom_openai_compatible",
            "model": "manual-model-id",
            "tool_calls": [],
        }
    )
    with session_factory() as session, pytest.raises(HTTPException) as rejected:
        ai_session_turn._finish_success(
            session, reservation, SUPERADMIN_PRINCIPAL, adapter_id, response
        )
    assert rejected.value.status_code == 409
    with session_factory() as session:
        count = session.scalar(
            text("SELECT count(*) FROM ai_conversation_messages WHERE conversation_id=:id"),
            {"id": uuid.UUID(session_id)},
        )
        assert count == 0


def test_concurrent_same_key_does_not_reserve_duplicate_user_row(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-pending-race")["id"])
    path, session_id = _session(api_client, adapter_id)
    body = _request(session_id)
    payload = AiAssistRequest.model_validate(body)
    with session_factory() as session:
        reservation = ai_session_turn._reserve_turn(
            session,
            adapter_id,
            payload,
            SUPERADMIN_PRINCIPAL,
            ai_session_turn._request_hmac(payload),
        )
        assert isinstance(reservation, ai_session_turn.TurnReservation)
        session.commit()
    concurrent = api_client.post(f"{path}/assist", json=body)
    assert concurrent.status_code == 409
    assert concurrent.json()["detail"]["code"] == "ai_session_in_progress"
    with session_factory() as session:
        count = session.scalar(
            text("SELECT count(*) FROM ai_conversation_messages WHERE conversation_id=:id"),
            {"id": uuid.UUID(session_id)},
        )
        assert count == 1


def test_clear_rejects_old_request_without_recreating_turn(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-clear-replay")["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    _fake_success(monkeypatch)
    frozen = _request(session_id)
    assert api_client.post(f"{path}/assist", json=frozen).status_code == 200
    assert api_client.post(f"{path}/sessions/{session_id}/clear").status_code == 200
    replay = api_client.post(f"{path}/assist", json=frozen)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "ai_session_stale_revision"
    with session_factory() as session:
        count = session.scalar(
            text("SELECT count(*) FROM ai_conversation_messages WHERE conversation_id=:id"),
            {"id": uuid.UUID(session_id)},
        )
        assert count == 0


def test_durable_candidate_strips_working_copy_configuration_before_save_and_replay(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-private-candidate")["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    private_config = {"credential": "private-runtime-value"}
    requirements = "private-package==1.2.3"

    def echo(*args: object, **kwargs: object) -> tuple[str, None]:
        output = valid_output()
        output["candidate"]["runtime_config"] = private_config
        output["candidate"]["requirements"] = requirements
        return json.dumps(output), None

    monkeypatch.setattr(providers, "chat_assist", echo)
    body = _request(session_id)
    body["working_copy"] = {
        "code": "def handle(context, input): return input",
        "runtime_config": private_config,
        "requirements": requirements,
    }
    first = api_client.post(f"{path}/assist", json=body)
    assert first.status_code == 200, first.text
    candidate = first.json()["candidate"]
    assert candidate["requirements"] == ""
    assert candidate["runtime_config"] == {}
    replay = api_client.post(f"{path}/assist", json=body)
    assert replay.status_code == 200 and replay.json() == first.json()
    with session_factory() as session:
        saved = session.scalar(
            text(
                "SELECT candidate_json FROM ai_conversation_messages "
                "WHERE conversation_id=:id AND role='assistant'"
            ),
            {"id": uuid.UUID(session_id)},
        )
        assert saved["requirements"] == ""
        assert saved["runtime_config"] == {}
        assert "private-runtime-value" not in json.dumps(saved)
        assert requirements not in json.dumps(saved)


def test_rollup_acl_error_after_reservation_marks_turn_failed(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-rollup-access") ["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)

    original_rollup = ai_session_turn._maybe_rollup
    original_access = ai_session_turn.adapter_access.require_adapter_access
    revoked = False

    def access_after_reservation(*args: object, **kwargs: object) -> object:
        if revoked:
            raise HTTPException(status_code=403, detail={"code": "adapter_access_denied"})
        return original_access(*args, **kwargs)

    def revoke_after_reservation(*args: object, **kwargs: object) -> object:
        nonlocal revoked
        revoked = True
        return original_rollup(*args, **kwargs)

    monkeypatch.setattr(
        ai_session_turn.adapter_access, "require_adapter_access", access_after_reservation
    )
    monkeypatch.setattr(ai_session_turn, "_maybe_rollup", revoke_after_reservation)
    response = api_client.post(f"{path}/assist", json=_request(session_id))
    assert response.status_code == 403
    with session_factory() as session:
        status = session.scalar(
            text(
                "SELECT request_status FROM ai_conversation_messages "
                "WHERE conversation_id=:id AND role='user'"
            ),
            {"id": uuid.UUID(session_id)},
        )
        assert status == "failed"


def test_late_old_generation_cannot_replace_newer_reply(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-generation-race")["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    _fake_success(monkeypatch)
    first_body = _request(session_id)
    assert api_client.post(f"{path}/assist", json=first_body).status_code == 200
    turn_id = uuid.UUID(str(first_body["turn_id"]))
    old_regen = AiAssistRequest.model_validate(
        _request(
            session_id,
            turn_id=turn_id,
            regenerate=True,
            expected_generation=1,
            expected_session_revision=_revision(api_client, path, session_id),
            message=str(first_body["message"]),
        )
    )
    with session_factory() as session:
        stale = ai_session_turn._reserve_turn(
            session,
            adapter_id,
            old_regen,
            SUPERADMIN_PRINCIPAL,
            ai_session_turn._request_hmac(old_regen),
        )
        assert isinstance(stale, ai_session_turn.TurnReservation)
        session.commit()
    newer_regen = AiAssistRequest.model_validate(
        _request(
            session_id,
            turn_id=turn_id,
            regenerate=True,
            expected_generation=2,
            expected_session_revision=_revision(api_client, path, session_id),
            message=str(first_body["message"]),
        )
    )
    with session_factory() as session:
        newer = ai_session_turn._reserve_turn(
            session,
            adapter_id,
            newer_regen,
            SUPERADMIN_PRINCIPAL,
            ai_session_turn._request_hmac(newer_regen),
        )
        assert isinstance(newer, ai_session_turn.TurnReservation)
        session.commit()
    response = AiAssistResponse.model_validate(
        {
            **valid_output(),
            "message": "New generation wins",
            "provider": "custom_openai_compatible",
            "model": "manual-model-id",
            "tool_calls": [],
        }
    )
    with session_factory() as session, pytest.raises(HTTPException) as rejected:
        ai_session_turn._finish_success(session, stale, SUPERADMIN_PRINCIPAL, adapter_id, response)
    assert isinstance(rejected.value.detail, dict)
    assert rejected.value.detail["code"] == "ai_session_stale"
    with session_factory() as session:
        ai_session_turn._finish_success(session, newer, SUPERADMIN_PRINCIPAL, adapter_id, response)
    detail = api_client.get(f"{path}/sessions/{session_id}").json()
    assert detail["messages"][1]["generation"] == 3
    assert detail["messages"][1]["content"] == "New generation wins"


def test_summary_failure_and_budget_refuse_uncovered_history(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-long-history")["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 100000)
    calls = {"assist": 0, "summary": 0}

    def fake_chat(*args: object, **kwargs: object) -> tuple[str | None, None]:
        messages = args[2]
        assert isinstance(messages, list)
        if "Summarize only" in str(messages[0]["content"]):
            calls["summary"] += 1
            raise providers.AiProviderError("ai_timeout")
        calls["assist"] += 1
        return json.dumps({"message": "reply " + "r" * 4000, "candidate": None}), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    for index in range(5):
        body = _request(
            session_id,
            message=f"Turn {index}: " + "u" * 4000,
            expected_session_revision=_revision(api_client, path, session_id),
        )
        response = api_client.post(f"{path}/assist", json=body)
        assert response.status_code == 200, response.text
    assert calls == {"assist": 5, "summary": 0}
    monkeypatch.setattr(settings, "ai_context_default_window_tokens", 12000)
    sixth = api_client.post(
        f"{path}/assist",
        json=_request(
            session_id,
            message="New current request",
            expected_session_revision=_revision(api_client, path, session_id),
        ),
    )
    assert sixth.status_code == 413, sixth.text
    assert sixth.json()["detail"]["code"] == "ai_session_context_incomplete"
    assert calls == {"assist": 5, "summary": 1}
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT summary_covered_through, next_sequence FROM ai_conversations WHERE id=:id"
            ),
            {"id": uuid.UUID(session_id)},
        ).one()
        assert row.summary_covered_through == 0
        assert row.next_sequence == 13
        count = session.scalar(
            text("SELECT count(*) FROM ai_conversation_messages WHERE conversation_id=:id"),
            {"id": uuid.UUID(session_id)},
        )
        assert count == 11  # six users, five assistants; originals retained


def test_valid_summary_enters_lower_priority_prompt_and_covered_regeneration_invalidates(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter_id = int(create_adapter(api_client, "durable-summary-wiring")["id"])
    configure(api_client)
    path, session_id = _session(api_client, adapter_id)
    captured_assist: list[list[dict[str, Any]]] = []

    def fake_chat(*args: object, **kwargs: object) -> tuple[str, None]:
        messages = args[2]
        assert isinstance(messages, list)
        if "Summarize only" in str(messages[0]["content"]):
            return json.dumps(
                {
                    "version": 1,
                    "text": "Earlier user requested no new dependencies.",
                    "sources": [
                        {"sequence": 1, "revision": 1, "role": "user"},
                        {"sequence": 2, "revision": 1, "role": "assistant"},
                    ],
                    "covered_through": 2,
                }
            ), None
        captured_assist.append(messages)
        return json.dumps({"message": "Done", "candidate": None}), None

    monkeypatch.setattr(providers, "chat_assist", fake_chat)
    first_body = _request(session_id, message="Do not add dependencies")
    assert api_client.post(f"{path}/assist", json=first_body).status_code == 200
    for index in range(2, 7):
        body = _request(
            session_id,
            message=f"Turn {index}",
            expected_session_revision=_revision(api_client, path, session_id),
        )
        assert api_client.post(f"{path}/assist", json=body).status_code == 200
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT summary_valid, summary_covered_through FROM ai_conversations WHERE id=:id"
            ),
            {"id": uuid.UUID(session_id)},
        ).one()
    assert row.summary_valid and row.summary_covered_through == 2
    # Own rollup changed the revision, but its initiating turn still committed.
    captured_count = len(captured_assist)
    sixth_replay = api_client.post(f"{path}/assist", json=body)
    assert sixth_replay.status_code == 200
    assert len(captured_assist) == captured_count
    sixth_messages = captured_assist[-1]
    current_user = sixth_messages[-1]["content"]
    assert "UNTRUSTED_CONVERSATION_CONTEXT" in str(current_user)
    assert "Earlier user requested no new dependencies" in str(current_user)
    assert "Do not add dependencies" not in str(sixth_messages[1:-1])

    regen = _request(
        session_id,
        turn_id=uuid.UUID(str(first_body["turn_id"])),
        regenerate=True,
        expected_generation=1,
        expected_session_revision=_revision(api_client, path, session_id),
        message=str(first_body["message"]),
        code="def handle(context, input): return 'latest working copy'",
    )
    assert api_client.post(f"{path}/assist", json=regen).status_code == 200
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT summary_valid, summary_covered_through FROM ai_conversations WHERE id=:id"
            ),
            {"id": uuid.UUID(session_id)},
        ).one()
        assert not row.summary_valid and row.summary_covered_through == 2
