"""Runtime preparation retains private snapshots and owns actual tool resources."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.ai import knowledge, providers
from dlr.control.ai import tools as tools_service
from dlr.control.ai.runtime.resources import ToolResourceFactory
from dlr.control.models import Adapter, Credential
from dlr.control.services import ai
from dlr.control.services import secrets as secrets_service
from test_ai import assist_body, configure, create_adapter, create_credential, fake_chat_response


def test_legacy_tool_uses_its_own_execution_thread_session_and_closes_it(
    api_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "runtime-owned-session")
    configure(api_client)
    observed: list[tuple[int, int]] = []
    owned: list[Any] = []
    route_sessions: list[Session] = []
    original_impl = ai._assist_impl

    def traced(session: Session, *args: Any, **kwargs: Any) -> Any:
        route_sessions.append(session)
        return original_impl(session, *args, **kwargs)

    class TrackedSession(Session):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.created_thread = threading.get_ident()
            self.did_close = False
            owned.append(self)

        def close(self) -> None:
            self.did_close = True
            super().close()

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        context = tools_service._TOOL_CONTEXT.get()
        assert isinstance(context, tools_service.ToolExecutionContext)
        assert isinstance(context.session, TrackedSession)
        assert context.session is not route_sessions[0]
        assert context.session.scalar(select(1)) == 1
        observed.append((context.session.created_thread, threading.get_ident()))
        return {"items": []}

    replies = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "owned-session",
                                    "type": "function",
                                    "function": {"name": "dlr_docs_list", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            fake_chat_response({"message": "Consultation complete", "candidate": None}),
        ]
    )
    monkeypatch.setattr(ai, "_assist_impl", traced)
    monkeypatch.setattr(ai, "Session", TrackedSession)
    monkeypatch.setitem(
        tools_service._TOOLS,
        "dlr_docs_list",
        replace(
            tools_service._TOOLS["dlr_docs_list"],
            handler=handler,
        ),
    )
    monkeypatch.setattr(providers, "_request_json", lambda *args, **kwargs: next(replies))
    body = assist_body()
    body["task_intent"] = "consultation"
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    assert len(owned) == 1 and owned[0].did_close
    assert len(observed) == 1 and observed[0][0] == observed[0][1]
    assert response.json()["task_status"] == "answered"


def test_source_snapshot_keeps_request_credentials_while_worker_session_observes_commit(
    api_client: Any,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_endpoint", "https://ima.qq.com")
    credential = create_credential(
        api_client,
        name="request-source-key",
        credential_type="access_key",
        fields={"access_key_id": "request-client", "access_key_secret": "request-key"},
    )
    saved = api_client.put(
        "/api/knowledge-sources/ima",
        json={
            "enabled": True,
            "credential_id": credential["id"],
        },
    )
    assert saved.status_code == 200
    with session_factory() as request_session:
        snapshot = knowledge.snapshot_source(request_session)
    assert "request-key" not in repr(snapshot)
    with session_factory() as writer:
        row = writer.get(Credential, credential["id"])
        assert row is not None
        row.ciphertext = secrets_service.encrypt_fields(
            {
                "access_key_id": "rotated-client",
                "access_key_secret": "rotated-key",
            }
        )
        writer.commit()

    factory = ToolResourceFactory(
        session_factory,
        tools_service.ToolExecutionContext(
            knowledge_search_enabled=True, source_factory=snapshot.build
        ),
    )
    main_thread = threading.get_ident()

    def run() -> tuple[str, ...]:
        assert threading.get_ident() != main_thread
        with factory.open() as context:
            row = context.session.get(Credential, credential["id"])
            assert row is not None
            assert (
                secrets_service.decrypt_fields(row.ciphertext)["access_key_secret"] == "rotated-key"
            )
            source = knowledge.build_source("ima", context)
            assert "request-key" not in repr(source)
            return source.redact_values()

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert set(pool.submit(run).result(timeout=3)) == {"request-client", "request-key"}
    with session_factory() as new_request:
        assert set(knowledge.snapshot_source(new_request).redact_values()) == {
            "rotated-client",
            "rotated-key",
        }


def test_plain_assist_rechecks_archival_before_return_without_another_provider_call(
    api_client: Any,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "runtime-final-permission")
    configure(api_client)
    calls = 0

    def upstream(*args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        with session_factory() as session:
            row = session.get(Adapter, adapter["id"])
            assert row is not None
            row.archived_at = datetime.now(UTC)
            session.commit()
        return fake_chat_response({"message": "Late answer", "candidate": None})

    monkeypatch.setattr(providers, "_request_json", upstream)
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "adapter_not_found"
    assert calls == 1
