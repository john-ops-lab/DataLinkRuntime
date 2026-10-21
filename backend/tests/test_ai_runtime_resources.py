"""Real PostgreSQL, SDK HTTP, Reader pins and current authorization."""

import base64
import hashlib
import json
import threading
import time
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ai_runtime_support import response, server
from dlr.common.config import settings
from dlr.control.ai import tools
from dlr.control.ai.attachment_store import principal_owner
from dlr.control.api import ai as ai_api
from dlr.control.models import User, UserSession
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import ai
from test_account_wave_c import account_write
from test_account_wave_c import wave_c_accounts as wave_c_accounts
from test_ai import assist_body, configure, create_adapter
from test_issue202_attachment_api import payload
from test_issue202_attachment_api import resources as resources
from test_issue202_attachment_store import PNG


@pytest.fixture(autouse=True)
def new_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_assist_runtime", "pydantic_ai")


def test_sdk_tool_constructs_and_closes_postgres_session_on_execution_thread(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    route: list[Session] = []
    owned: list[Any] = []
    observed: list[int] = []
    prepare = ai._prepare_assist

    def traced(session: Session, *args: Any, **kwargs: Any) -> Any:
        route.append(session)
        return prepare(session, *args, **kwargs)

    class TrackedSession(Session):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.created_thread, self.did_close = threading.get_ident(), False
            owned.append(self)

        def close(self) -> None:
            self.did_close = True
            super().close()

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        context = tools.current_tool_context()
        assert context is not None and isinstance(context.session, TrackedSession)
        assert context.session is not route[0]
        assert context.session.created_thread == threading.get_ident()
        assert context.session.scalar(select(1)) == 1
        observed.append(threading.get_ident())
        return {"items": []}

    monkeypatch.setattr(ai, "_prepare_assist", traced)
    monkeypatch.setattr(ai, "Session", TrackedSession)
    monkeypatch.setitem(
        tools._TOOLS, "dlr_docs_list", replace(tools._TOOLS["dlr_docs_list"], handler=handler)
    )
    with server(
        response("openai", calls=[("dlr_docs_list", "one", "{}")]), response("openai")
    ) as script:
        adapter = create_adapter(api_client, "owned-sdk-session")
        configure(api_client, base_url=script.url)
        result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert result.status_code == 200, result.text
    assert len(owned) == len(observed) == 1
    assert owned[0].did_close


@pytest.mark.parametrize("change,expected", [("disable", 401), ("logout", 401), ("role", 404)])
def test_current_account_authority_rechecked_before_return(
    api_client: Any, session_factory: Any, wave_c_accounts: Any, change: str, expected: int
) -> None:
    user_id = wave_c_accounts["account_admin"]["id"]
    adapter = create_adapter(api_client, "live-authority")

    def revoke(request: Any) -> Any:
        with session_factory() as writer:
            user = writer.get(User, user_id)
            assert user is not None
            if change == "disable":
                user.enabled = False
            elif change == "role":
                user.role = "user"
            else:
                writer.execute(delete(UserSession).where(UserSession.user_id == user_id))
            writer.commit()
        return response("openai")

    with server(responder=revoke) as script:
        configure(api_client, base_url=script.url)
        result = account_write(
            wave_c_accounts["account_admin_client"],
            "POST",
            f"/api/adapters/{adapter['id']}/ai/assist",
            json=assist_body(),
        )
        assert result.status_code == expected, result.text
        assert len(script.requests) == 1


def test_timed_out_real_tool_keeps_session_and_reader_until_physical_end(
    api_client: Any, resources: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = resources
    monkeypatch.setattr(ai_api, "get_attachment_store", lambda: store)
    adapter = create_adapter(api_client, "owned-sdk-reader")
    scope = str(uuid4())
    ready = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/attachments", json=payload(scope)
    ).json()
    ref = ready["attachment_ref"]
    record = store._records[ref]
    directory = record.directory
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_prepare = ai._prepare_assist
    original_handler = tools._TOOLS["read_attachment_page"].handler

    def prepare(*args: Any, **kwargs: Any) -> Any:
        context = original_prepare(*args, **kwargs)
        now = time.monotonic()
        context.state.tool_deadline, context.state.hard_deadline = now + 0.5, now + 2
        return context

    def slow(args: dict[str, Any]) -> Any:
        context = tools.current_tool_context()
        assert context is not None and context.session is not None
        assert context.session.scalar(select(1)) == 1
        assert record.pins == 2
        entered.set()
        try:
            assert release.wait(4)
            return original_handler(args)
        finally:
            finished.set()

    monkeypatch.setattr(ai, "_prepare_assist", prepare)
    monkeypatch.setitem(
        tools._TOOLS,
        "read_attachment_page",
        replace(tools._TOOLS["read_attachment_page"], handler=slow),
    )
    args = json.dumps({"attachment_ref": ref, "page": 1, "view": "text"})
    try:
        with server(
            response("openai", calls=[("read_attachment_page", "owned-read", args)])
        ) as script:
            configure(api_client, base_url=script.url)
            body = {
                **assist_body(),
                "task_intent": "code_change",
                "attachment_scope_id": scope,
                "attachment_refs": [
                    {
                        key: ready[key]
                        for key in ("attachment_ref", "sha256", "representation_revision")
                    }
                ],
            }
            result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
            assert result.status_code == 200, result.text
            assert entered.is_set() and not finished.is_set()
            assert result.json()["task_status"] == "incomplete"
            assert result.json()["candidate"] is None
            assert result.json()["tool_calls"][0]["error_code"] == "ai_tool_timeout"
            assert len(script.requests) == 1
        assert record.pins == 1
        store.release(principal_owner(SUPERADMIN_PRINCIPAL), adapter["id"], scope)
        assert directory.exists() and record.pins == 1
        release.set()
        assert finished.wait(2)
        deadline = time.monotonic() + 2
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not directory.exists()
        assert ref not in store._records
    finally:
        release.set()


@pytest.mark.parametrize("protocol", ["openai", "anthropic", "google"])
def test_scoped_tool_media_preserves_bytes_provenance_and_round(
    api_client: Any, resources: Any, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    store, _ = resources
    monkeypatch.setattr(ai_api, "get_attachment_store", lambda: store)
    adapter = create_adapter(api_client, "sdk-media-" + protocol)
    scope = str(uuid4())
    ready = api_client.post(
        f"/api/adapters/{adapter['id']}/ai/attachments", json=payload(scope)
    ).json()
    arguments = json.dumps({"attachment_ref": ready["attachment_ref"], "page": 1, "view": "image"})
    with server(
        response(protocol, calls=[("read_attachment_page", "original-media-call", arguments)]),
        response(protocol),
    ) as script:
        profile = api_client.post(
            "/api/ai/custom-providers",
            json={
                "name": "sdk-media-profile",
                "protocol": "gemini"
                if protocol == "google"
                else "openai_compatible"
                if protocol == "openai"
                else "anthropic",
                "base_url": script.url,
                "images_native": True,
                "tools_supported": True,
            },
        )
        assert profile.status_code == 200, profile.text
        configure(api_client, base_url=script.url, custom_provider_id=profile.json()["id"])
        body = {
            **assist_body(),
            "task_intent": "consultation",
            "attachment_scope_id": scope,
            "attachment_refs": [
                {key: ready[key] for key in ("attachment_ref", "sha256", "representation_revision")}
            ],
        }
        result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
        assert result.status_code == 200, result.text
        assert result.json()["task_status"] == "answered"
        assert [item["status"] for item in result.json()["tool_calls"]] == ["success"]
        assert len(script.requests) == 2
        found: list[bytes] = []

        def pixels(value: Any) -> None:
            if isinstance(value, dict):
                if value.get(
                    "mimeType", value.get("mime_type", value.get("media_type"))
                ) == "image/png" and isinstance(value.get("data"), str):
                    found.append(base64.urlsafe_b64decode(value["data"]))
                if value.get("type") == "image_url":
                    found.append(
                        base64.b64decode(value["image_url"]["url"].split(";base64,", 1)[1])
                    )
                for item in value.values():
                    pixels(item)
            elif isinstance(value, list):
                for item in value:
                    pixels(item)

        pixels(script.requests[1].json())
        assert found == [PNG]
        encoded = script.requests[1].body.decode()
        assert ready["attachment_ref"] in encoded and ready["sha256"] in encoded
        assert hashlib.sha256(PNG).hexdigest() in encoded
        assert "original-media-call" in encoded
        assert "dlr_internal_dispatch" not in encoded
        assert store._records[ready["attachment_ref"]].pins == 0
