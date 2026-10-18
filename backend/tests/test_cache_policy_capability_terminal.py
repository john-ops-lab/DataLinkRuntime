"""Policy capability loss terminates dispatch without claiming an application outcome."""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import func, select

from dlr.control.models import Worker, WorkerCacheManagementOperation, WorkerCacheSnapshotItem
from dlr.control.services import cache_admin
from test_issue161_cache_admin import WORKER_HEADERS, _upload
from test_issue161_cache_governance import _fixture

UNSUPPORTED_RESULT = {"summary": "Worker does not support cache policy updates"}


def _setup(api_client, session_factory, monkeypatch):
    worker, adapter, version = _fixture(api_client, session_factory, monkeypatch, "policy-terminal")
    worker_id = int(worker["id"])
    _upload(api_client, worker_id, int(adapter["id"]), version)
    return worker_id, f"/api/workers/{worker_id}/cache/operations"


def _capability(session_factory, worker_id, enabled):
    with session_factory.begin() as session:
        worker = session.get(Worker, worker_id)
        worker.isolation_capabilities = {
            **worker.isolation_capabilities,
            "cache_policy_control_v1": enabled,
        }


def _create(api_client, url, **request):
    response = api_client.post(url, json=request)
    assert response.status_code == 202, response.text
    return response.json()


def _claim(api_client, worker_id):
    return api_client.post(f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS)


def _assert_terminal(api_client, url, request, operation_id, *, claim_epoch=0):
    # The original idempotency key returns its terminal audit even while unsupported.
    audit = _create(api_client, url, **request)
    assert audit["operation_id"] == operation_id
    assert audit["status"] == "failed"
    assert audit["error_code"] == "cache_policy_unsupported"
    assert audit["finished_at"] is not None
    assert audit["claim_epoch"] == claim_epoch
    assert audit["result"] == UNSUPPORTED_RESULT
    cache_admin._safe_result(audit["result"])
    return audit


def test_policy_creation_without_capability_is_rejected_without_enqueue(
    api_client, session_factory, monkeypatch
):
    worker_id, url = _setup(api_client, session_factory, monkeypatch)
    response = api_client.post(
        url,
        json={"kind": "protect", "idempotency_key": "unsupported", "policy": {"gc_enabled": True}},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "cache_policy_unsupported"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(WorkerCacheManagementOperation)) == 0
    assert _claim(api_client, worker_id).status_code == 204


def test_queued_policy_downgrade_is_terminal_and_releases_active_gate(
    api_client, session_factory, monkeypatch
):
    worker_id, url = _setup(api_client, session_factory, monkeypatch)
    _capability(session_factory, worker_id, True)
    request = {"kind": "protect", "idempotency_key": "policy", "policy": {"gc_enabled": True}}
    created = _create(api_client, url, **request)
    _capability(session_factory, worker_id, False)
    assert _claim(api_client, worker_id).status_code == 204
    terminal = _assert_terminal(api_client, url, request, created["operation_id"])
    assert terminal["claimed_at"] is None
    assert terminal["request"] == created["request"]
    assert _claim(api_client, worker_id).status_code == 204
    assert _assert_terminal(api_client, url, request, created["operation_id"]) == terminal
    conflict = api_client.post(url, json={**request, "policy": {"gc_enabled": False}})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "cache_idempotency_conflict"
    # Ordinary governance operations remain usable on the older Worker.
    with session_factory() as session:
        item = session.scalar(
            select(WorkerCacheSnapshotItem).where(WorkerCacheSnapshotItem.worker_id == worker_id)
        )
        keys = [{"adapter_id": item.adapter_id, "version_id": item.version_id}]
    followup = _create(api_client, url, kind="preview", idempotency_key="preview", keys=keys)
    claim = _claim(api_client, worker_id)
    assert claim.status_code == 200, claim.text
    assert claim.json()["operation_id"] == followup["operation_id"]
    assert "policy" not in claim.json()["payload"]


@pytest.mark.parametrize("retry_depth", [1, 2])
def test_retry_resolves_policy_ancestor_and_can_resume_with_restored_capability(
    api_client, session_factory, monkeypatch, retry_depth
):
    worker_id, url = _setup(api_client, session_factory, monkeypatch)
    _capability(session_factory, worker_id, True)
    request = {"kind": "protect", "idempotency_key": "root", "policy": {"gc_enabled": True}}
    root = _create(api_client, url, **request)
    _capability(session_factory, worker_id, False)
    assert _claim(api_client, worker_id).status_code == 204
    target = root
    for depth in range(retry_depth):
        retry = {
            "kind": "retry",
            "idempotency_key": f"retry-{depth}",
            "management_operation_id": target["operation_id"],
        }
        target = _create(api_client, url, **retry)
        assert _claim(api_client, worker_id).status_code == 204
        _assert_terminal(api_client, url, retry, target["operation_id"])
    _capability(session_factory, worker_id, True)
    resumed = _create(
        api_client,
        url,
        kind="retry",
        idempotency_key="resume",
        management_operation_id=target["operation_id"],
    )
    claim = _claim(api_client, worker_id)
    assert claim.status_code == 200, claim.text
    command = claim.json()
    assert command["operation_id"] == resumed["operation_id"]
    assert command["claim_epoch"] == 1
    assert command["payload"]["retry_root_operation_id"] == root["operation_id"]
    assert command["payload"]["retry_command"]["payload"] == root["request"]


def test_recovered_retry_preserves_claim_evidence_without_inventing_application_result(
    api_client, session_factory, monkeypatch
):
    worker_id, url = _setup(api_client, session_factory, monkeypatch)
    _capability(session_factory, worker_id, True)
    root = _create(
        api_client, url, kind="protect", idempotency_key="root", policy={"gc_enabled": True}
    )
    _capability(session_factory, worker_id, False)
    assert _claim(api_client, worker_id).status_code == 204
    _capability(session_factory, worker_id, True)
    request = {
        "kind": "retry",
        "idempotency_key": "recovered",
        "management_operation_id": root["operation_id"],
    }
    retry = _create(api_client, url, **request)
    assert _claim(api_client, worker_id).status_code == 200
    with session_factory.begin() as session:
        operation = session.get(WorkerCacheManagementOperation, uuid.UUID(retry["operation_id"]))
        claimed_at = operation.claimed_at
        # Registration recovery requeues a dispatched command whose Worker result is unknown.
        cache_admin.recover_running_operations(session, worker_id)
    _capability(session_factory, worker_id, False)
    assert _claim(api_client, worker_id).status_code == 204
    terminal = _assert_terminal(api_client, url, request, retry["operation_id"], claim_epoch=1)
    assert datetime.fromisoformat(terminal["claimed_at"]) == claimed_at
    assert terminal["target_operation_id"] == root["operation_id"]
    with session_factory() as session:
        root_audit = session.get(WorkerCacheManagementOperation, uuid.UUID(root["operation_id"]))
        assert root_audit.status == "failed"
        assert root_audit.claim_epoch == 0
