"""Prepared resources require both Adapter edit permission and the page owner."""

import base64
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from conftest import WORKER_TOKEN
from dlr.control.ai.attachment_store import AiAttachmentStore, get_attachment_store
from dlr.control.api import ai as ai_api
from dlr.control.schemas.ai import AiAssistResponse
from dlr.control.services import ai as ai_service
from test_account_wave_c import (
    account_path,
    account_write,
    create_account_adapter,
    create_adapter,
    grant,
    wave_c_accounts,
)
from test_ai import assist_body
from test_issue202_attachment_store import Converter

assert wave_c_accounts  # Register the shared account fixture in this module.


@pytest.fixture
def resources(api_client: TestClient, tmp_path) -> Iterator[tuple[AiAttachmentStore, Converter]]:
    converter = Converter()
    store = AiAttachmentStore(tmp_path / "resources", converter)
    api_client.app.dependency_overrides[get_attachment_store] = lambda: store
    yield store, converter
    store.close()


def payload(scope: str | None = None) -> dict[str, Any]:
    return {
        "scope_id": scope or str(uuid4()),
        "attachment": {
            "filename": "business.txt",
            "content_type": "text/plain",
            "data_base64": base64.b64encode(b"authorized business reference").decode(),
        },
    }


def test_edit_acl_and_principal_owner_are_independent(
    api_client: TestClient, resources, wave_c_accounts
) -> None:
    store, converter = resources
    owner = wave_c_accounts["owner_client"]
    editor = wave_c_accounts["editor_client"]
    reader = wave_c_accounts["reader_client"]
    adapter = create_account_adapter(owner, "prepared-owner")
    adapter_id = adapter["id"]
    path = f"/api/adapters/{adapter_id}/ai/attachments"
    assert grant(owner, adapter_id, wave_c_accounts["editor"]["id"], "edit").status_code == 200
    assert grant(owner, adapter_id, wave_c_accounts["reader"]["id"], "read").status_code == 200
    body = payload()
    rejected = account_write(reader, "POST", path, json=body)
    assert rejected.status_code == 403 and converter.calls == 0
    ready = account_write(owner, "POST", path, json=body)
    assert ready.status_code == 200, ready.text
    result = ready.json()
    ref_path = f"{path}/{result['attachment_ref']}?scope_id={body['scope_id']}"
    assert owner.get(account_path(ref_path)).status_code == 200
    # Edit permission does not make another Principal's locators readable.
    assert editor.get(account_path(ref_path)).status_code == 410
    assert api_client.get(ref_path).status_code == 410
    assert account_write(editor, "DELETE", ref_path).status_code == 204
    assert owner.get(account_path(ref_path)).status_code == 200
    other = create_account_adapter(owner, "prepared-other-adapter")
    assert (
        owner.get(
            account_path(ref_path.replace(f"/{adapter_id}/", f"/{other['id']}/", 1))
        ).status_code
        == 410
    )
    # Losing edit permission blocks future reads/releases even in the same page scope.
    assert grant(owner, adapter_id, wave_c_accounts["editor"]["id"], "read").status_code == 200
    assert editor.get(account_path(ref_path)).status_code == 403
    assert account_write(owner, "DELETE", ref_path).status_code == 204
    assert owner.get(account_path(ref_path)).status_code == 410
    assert not store._records and store._used_bytes == 0


def test_worker_and_anonymous_cannot_prepare(api_client: TestClient, resources) -> None:
    _, converter = resources
    adapter = create_adapter(api_client, "prepared-auth")
    path = f"/api/adapters/{adapter['id']}/ai/attachments"
    worker = api_client.post(
        path, json=payload(), headers={"Authorization": f"Bearer {WORKER_TOKEN}"}
    )
    assert worker.status_code == 401
    anonymous = TestClient(api_client.app)
    response = anonymous.post(path, json=payload())
    anonymous.close()
    assert response.status_code == 401
    assert converter.calls == 0


def test_invalid_body_is_redacted_and_does_not_poison_next_prepare(
    api_client: TestClient, resources
) -> None:
    store, converter = resources
    adapter = create_adapter(api_client, "prepared-recovery")
    path = f"/api/adapters/{adapter['id']}/ai/attachments"
    body = payload()
    body["scope_id"] = "private-invalid-scope"
    body["attachment"]["data_base64"] = "PRIVATE-INVALID-BASE64"
    response = api_client.post(path, json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ai_attachment_invalid"
    assert "PRIVATE" not in response.text and "private-invalid" not in response.text
    assert converter.calls == 0 and store._used_bytes == 0
    good = payload()
    ready = api_client.post(path, json=good)
    assert ready.status_code == 200, ready.text
    release = api_client.delete(f"{path}/scopes/{good['scope_id']}")
    assert release.status_code == 204 and store._used_bytes == 0
    # A delayed upload from the released generation cannot recreate its scope.
    assert api_client.post(path, json=good).status_code == 409
    assert api_client.post(path, json=payload()).status_code == 200


def test_disconnected_prepare_cancels_and_reclaims_before_return(
    api_client: TestClient, resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, converter = resources
    converter.block = True

    async def disconnected(request: Request) -> bool:
        return converter.entered.is_set()

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    adapter = create_adapter(api_client, "prepared-disconnect")
    path = f"/api/adapters/{adapter['id']}/ai/attachments"
    response = api_client.post(path, json=payload())
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ai_attachment_cancelled"
    assert not store._records and store._used_bytes == 0
    assert list(store.directory.iterdir()) == [store.directory / ".dlr-ai-owner.json"]
    converter.block = False
    assert api_client.post(path, json=payload()).status_code == 200


def frozen_body(scope: str, resource: dict[str, Any]) -> dict[str, Any]:
    return {
        **assist_body(),
        "attachment_scope_id": scope,
        "attachment_refs": [
            {key: resource[key] for key in ("attachment_ref", "sha256", "representation_revision")}
        ],
    }


def test_assist_pins_only_its_frozen_resource_and_always_releases_pin(
    api_client: TestClient, resources, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = resources
    monkeypatch.setattr(ai_api, "get_attachment_store", lambda: store)
    adapter = create_adapter(api_client, "prepared-assist")
    path = f"/api/adapters/{adapter['id']}/ai"
    prepare_body = payload()
    ready = api_client.post(path + "/attachments", json=prepare_body).json()
    body = frozen_body(prepare_body["scope_id"], ready)
    calls = []

    def assist(
        session, adapter_id, request, *, attachment_reader, reader_factory, permission_check
    ):
        calls.append(adapter_id)
        record = store._records[ready["attachment_ref"]]
        assert record.pins == 1
        with reader_factory() as owned_reader:
            assert record.pins == 2
            owned_identity = owned_reader.overview()["attachments"][0]
            outer_identity = attachment_reader.overview()["attachments"][0]
            for key in ("attachment_ref", "sha256", "representation_revision"):
                assert owned_identity[key] == outer_identity[key] == ready[key]
        assert record.pins == 1
        permission_check()
        assert attachment_reader.overview()["attachments"][0]["sha256"] == ready["sha256"]
        page = attachment_reader.read_page(ready["attachment_ref"], 3, "both", timeout_seconds=2)
        assert page.data["page"] == 3 and page.media[0].page == 3
        return AiAssistResponse(
            message="Read original page 3",
            candidate=None,
            request_intent="consultation",
            task_status="answered",
            provider="minimax",
            model="MiniMax-M3",
        )

    monkeypatch.setattr(ai_service, "assist", assist)
    bad = frozen_body(prepare_body["scope_id"], {**ready, "sha256": "0" * 64})
    rejected = api_client.post(path + "/assist", json=bad)
    assert rejected.status_code == 409 and calls == []
    response = api_client.post(path + "/assist", json=body)
    assert response.status_code == 200, response.text
    assert store._records[ready["attachment_ref"]].pins == 0
    assert "data_base64" not in response.text
    assert (
        api_client.delete(path + "/attachments/scopes/" + prepare_body["scope_id"]).status_code
        == 204
    )
    assert api_client.post(path + "/assist", json=body).status_code == 410
    assert len(calls) == 1 and store._used_bytes == 0


def test_assist_observes_real_acl_revocation_even_on_cached_read(
    api_client: TestClient, resources, wave_c_accounts, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = resources
    monkeypatch.setattr(ai_api, "get_attachment_store", lambda: store)
    owner, editor = wave_c_accounts["owner_client"], wave_c_accounts["editor_client"]
    adapter = create_account_adapter(owner, "prepared-live-acl")
    adapter_id = adapter["id"]
    editor_id = wave_c_accounts["editor"]["id"]
    assert grant(owner, adapter_id, editor_id, "edit").status_code == 200
    path = f"/api/adapters/{adapter_id}/ai"
    prepare_body = payload()
    ready = account_write(editor, "POST", path + "/attachments", json=prepare_body).json()

    def assist(
        session, adapter_id, request, *, attachment_reader, reader_factory, permission_check
    ):
        attachment_reader.read_page(ready["attachment_ref"], 3, "both", timeout_seconds=2)
        assert grant(owner, adapter_id, editor_id, "read").status_code == 200
        # This is a cache hit, and must still query the changed database ACL.
        return attachment_reader.read_page(ready["attachment_ref"], 3, "both", timeout_seconds=2)

    monkeypatch.setattr(ai_service, "assist", assist)
    response = account_write(
        editor, "POST", path + "/assist", json=frozen_body(prepare_body["scope_id"], ready)
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "adapter_read_only"
    assert store._records[ready["attachment_ref"]].pins == 0
