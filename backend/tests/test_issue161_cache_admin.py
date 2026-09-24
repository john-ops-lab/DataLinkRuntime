"""Focused durable administrator cache operation contracts."""

import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import (
    WorkerCacheManagementChild,
    WorkerCacheManagementOperation,
    WorkerCacheOperation,
    WorkerCacheSnapshot,
    WorkerCleanupRequest,
)
from dlr.control.schemas.cache_admin import CacheOperationCreate
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import cache_admin as cache_admin_service
from dlr.worker.agent import Agent
from dlr.worker.cache_lifecycle import CacheLifecycleStore
from dlr.worker.cache_policy import CachePolicy
from dlr.worker.client import ControlUnavailableError
from runtime_api_support import ready_registration
from test_issue161_cache_deletion import FakeGuardClient
from test_issue161_cache_governance import _fixture
from test_issue161_cache_policy import _manager as policy_manager
from test_issue161_cache_policy import _ready as policy_ready

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


class _CommandClient:
    def __init__(self) -> None:
        self.reports: list[dict[str, object]] = []

    def report_cache_command(self, _worker_id: int, _operation_id: str, **payload) -> None:
        self.reports.append(payload)


class _SecondKeyUnavailable(FakeGuardClient):
    def check_cache_guard(self, worker_id, operation_id, *, timeout_seconds=None):
        if self.operations[operation_id]["version_id"] == 14 and self.fail_checks:
            raise ControlUnavailableError("owned second key unavailable")
        failed = self.fail_checks
        self.fail_checks = False
        try:
            return super().check_cache_guard(
                worker_id, operation_id, timeout_seconds=timeout_seconds
            )
        finally:
            self.fail_checks = failed


def _local_agent(lifecycle, manager) -> Agent:
    agent = Agent.__new__(Agent)
    agent._cache_lifecycle = lifecycle
    agent._cache_policy_manager = manager
    agent._cache_deletion_manager = manager.deletion
    agent._client = _CommandClient()
    agent._recover_cache_deletions = lambda: [
        manager.deletion.recover_round(max_items=20, max_pages=0) for _ in range(3)
    ]
    return agent


def _summary() -> dict[str, object]:
    category = {"entries": 0, "bytes": 0, "reclaimable_bytes": 0, "reasons": {}}
    return {
        "accounting": {"committed_bytes": 123, "reserved_bytes": 0},
        "categories": {
            name: dict(category) for name in ("versions", "shared", "staging", "trash", "unknown")
        },
        "retained_reasons": {},
        "policy": {
            "gc_enabled": False,
            "pressure_gc_enabled": False,
            "scan_interval_seconds": 300,
            "idle_ttl_seconds": 2_592_000,
            "min_idle_seconds": 86_400,
            "max_bytes": 4_294_967_296,
            "high_watermark_percent": 85,
            "low_watermark_percent": 70,
            "disk_reserve_bytes": 134_217_728,
            "max_delete_bytes_per_round": 268_435_456,
            "max_delete_entries_per_round": 20,
            "max_scan_entries_per_round": 200,
            "max_scan_nodes_per_round": 100_000,
            "max_scan_hash_bytes_per_round": 268_435_456,
            "max_scan_depth": 64,
            "max_round_seconds": 10,
            "staging_ttl_seconds": 86_400,
            "offline_protection": True,
            "offline_mode": False,
            "shared_cache_mode": "report_only",
        },
    }


def _upload(api_client: TestClient, worker_id: int, adapter_id: int, version_id: int) -> None:
    response = api_client.post(
        f"/api/workers/{worker_id}/cache/snapshot",
        headers=WORKER_HEADERS,
        json={
            "sample_id": str(uuid.uuid4()),
            "sequence": 1,
            "sampled_at": datetime.now(UTC).isoformat(),
            "state": "complete",
            "complete": True,
            "summary": _summary(),
            "items": [
                {
                    "cache_key": f"{adapter_id}-{version_id}",
                    "adapter_id": adapter_id,
                    "version_id": version_id,
                    "identity": {
                        "adapter_id": adapter_id,
                        "version_id": version_id,
                        "language": "python",
                        "source_sha256": "a" * 64,
                    },
                    "digest": "b" * 64,
                    "bytes": 123,
                    "pinned": False,
                    "rebuildability": "unknown",
                    "reasons": ["cache_rebuild_unknown"],
                }
            ],
        },
    )
    assert response.status_code == 204, response.text


def test_admin_operation_claim_fencing_and_idempotency(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "admin")
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    _upload(api_client, worker_id, adapter_id, version_id)
    payload = {
        "kind": "preview",
        "idempotency_key": "preview-1",
        "keys": [{"adapter_id": adapter_id, "version_id": version_id}],
    }
    created = api_client.post(f"/api/workers/{worker_id}/cache/operations", json=payload)
    assert created.status_code == 202, created.text
    repeated = api_client.post(f"/api/workers/{worker_id}/cache/operations", json=payload)
    assert repeated.status_code == 202
    assert repeated.json()["operation_id"] == created.json()["operation_id"]
    conflict = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={**payload, "keys": [{"adapter_id": adapter_id, "version_id": version_id + 1}]},
    )
    assert conflict.status_code == 409

    claim = api_client.post(
        f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS
    )
    assert claim.status_code == 200
    command = claim.json()
    assert command["claim_epoch"] == 1
    result_url = f"/api/workers/{worker_id}/cache/commands/{command['operation_id']}/result"
    registration = ready_registration(str(worker["name"]), ["python"])
    registration["isolation_capabilities"]["cache_governance_v1"] = True
    assert (
        api_client.post(
            "/api/workers/register", json=registration, headers=WORKER_HEADERS
        ).status_code
        == 200
    )
    reclaimed = api_client.post(
        f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS
    )
    assert reclaimed.status_code == 200
    assert reclaimed.json()["operation_id"] == command["operation_id"]
    assert reclaimed.json()["claim_epoch"] == 2
    stale = api_client.post(
        result_url,
        headers=WORKER_HEADERS,
        json={
            "claim_epoch": 1,
            "request_hash": command["request_hash"],
            "status": "completed",
            "result": {"complete": True},
        },
    )
    assert stale.status_code == 409
    accepted = api_client.post(
        result_url,
        headers=WORKER_HEADERS,
        json={
            "claim_epoch": 2,
            "request_hash": command["request_hash"],
            "status": "completed",
            "result": {"complete": True},
        },
    )
    assert accepted.status_code == 204
    assert (
        api_client.post(
            result_url,
            headers=WORKER_HEADERS,
            json={
                "claim_epoch": 2,
                "request_hash": command["request_hash"],
                "status": "completed",
                "result": {"complete": True},
            },
        ).status_code
        == 204
    )


def test_snapshot_is_bounded_fresh_and_admin_only(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "snapshot")
    worker_id = int(worker["id"])
    _upload(api_client, worker_id, int(adapter["id"]), version_id)
    view = api_client.get(f"/api/workers/{worker_id}/cache")
    assert view.status_code == 200
    assert view.json()["status"] == "complete"
    assert view.json()["sample_id"]
    assert len(view.json()["items"]) == 1
    assert TestClient(api_client.app).get(f"/api/workers/{worker_id}/cache").status_code == 401

    with session_factory.begin() as session:
        snapshot = session.get(WorkerCacheSnapshot, worker_id)
        assert snapshot is not None
        snapshot.received_at = datetime.now(UTC) - timedelta(seconds=301)
    assert api_client.get(f"/api/workers/{worker_id}/cache").json()["status"] == "stale"
    stale_operation = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "preview",
            "idempotency_key": "stale-preview",
            "keys": [{"adapter_id": int(adapter["id"]), "version_id": version_id}],
        },
    )
    assert stale_operation.status_code == 409
    assert stale_operation.json()["detail"]["code"] == "cache_snapshot_unavailable"


def test_two_transactions_allow_only_one_active_operation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "concurrent")
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    _upload(api_client, worker_id, adapter_id, version_id)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def create(number: int) -> None:
        with session_factory() as session:
            barrier.wait()
            try:
                cache_admin_service.create_operation(
                    session,
                    worker_id,
                    CacheOperationCreate.model_validate(
                        {
                            "kind": "preview",
                            "idempotency_key": f"concurrent-{number}",
                            "keys": [{"adapter_id": adapter_id, "version_id": version_id}],
                        }
                    ),
                    SUPERADMIN_PRINCIPAL,
                )
                results.append("created")
            except HTTPException as error:
                results.append(str(error.detail["code"]))

    threads = [threading.Thread(target=create, args=(number,)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert results.count("created") == 1
    assert results.count("cache_operation_active") == 1


def test_operation_dto_rejects_unbounded_duplicate_and_mixed_targets(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, _adapter, _version_id = _fixture(api_client, session_factory, monkeypatch, "dto")
    worker_id = int(worker["id"])
    too_many = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "preview",
            "idempotency_key": "too-many",
            "keys": [{"adapter_id": 1, "version_id": number} for number in range(1, 202)],
        },
    )
    assert too_many.status_code == 422
    duplicate = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "clean",
            "idempotency_key": "duplicate",
            "keys": [{"adapter_id": 1, "version_id": 1}] * 2,
        },
    )
    assert duplicate.status_code == 422
    mixed = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "retry",
            "idempotency_key": "mixed",
            "cleanup_id": 1,
            "guard_operation_id": str(uuid.uuid4()),
            "path": "/tmp/forbidden",
        },
    )
    assert mixed.status_code == 422
    with session_factory() as session:
        assert session.scalar(select(WorkerCacheManagementOperation)) is None


def test_guard_retry_rejects_replacement_operation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "replacement-retry"
    )
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    _upload(api_client, worker_id, adapter_id, version_id)
    guard_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            WorkerCacheOperation(
                operation_id=guard_id,
                worker_id=worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                generation=1,
                phase="acquired",
                operation_kind="replacement",
                replacement_context={"schema": 1},
            )
        )
        snapshot = session.get(WorkerCacheSnapshot, worker_id)
        assert snapshot is not None
        snapshot.summary = {
            **snapshot.summary,
            "failed_guard_items": [
                {
                    "guard_operation_id": str(guard_id),
                    "generation": 1,
                    "adapter_id": adapter_id,
                    "version_id": version_id,
                    "operation_kind": "replacement",
                    "local_phase": "failed",
                    "resume_phase": "recorded",
                    "failure_count": 3,
                    "error_code": "cache_cleanup_failed",
                    "sampled_at": datetime.now(UTC).isoformat(),
                }
            ],
        }
    response = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "retry",
            "idempotency_key": "replacement-retry",
            "guard_operation_id": str(guard_id),
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "cache_retry_target_unavailable"

    gc_guard_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            WorkerCacheOperation(
                operation_id=gc_guard_id,
                worker_id=worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                generation=2,
                phase="acquired",
                operation_kind="gc",
            )
        )
        snapshot = session.get(WorkerCacheSnapshot, worker_id)
        assert snapshot is not None
        snapshot.summary = {
            **snapshot.summary,
            "failed_guard_items": [
                {
                    "guard_operation_id": str(gc_guard_id),
                    "generation": 2,
                    "adapter_id": adapter_id,
                    "version_id": version_id,
                    "operation_kind": "gc",
                    "local_phase": "failed",
                    "resume_phase": "recorded",
                    "failure_count": 3,
                    "error_code": "cache_cleanup_failed",
                    "sampled_at": datetime.now(UTC).isoformat(),
                }
            ],
        }
    accepted = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "retry",
            "idempotency_key": "gc-retry",
            "guard_operation_id": str(gc_guard_id),
        },
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["target_kind"] == "guard"
    assert accepted.json()["target_operation_id"] == str(gc_guard_id)


def test_management_retry_rejects_children_already_resolved(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "resolved-management-retry"
    )
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    parent_id = uuid.uuid4()
    guard_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            WorkerCacheOperation(
                operation_id=guard_id,
                worker_id=worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                generation=1,
                phase="completed",
                operation_kind="gc",
            )
        )
        session.add(
            WorkerCacheManagementOperation(
                operation_id=parent_id,
                worker_id=worker_id,
                kind="clean",
                status="failed",
                idempotency_key="resolved-parent",
                request_hash="a" * 64,
                request_payload={
                    "kind": "clean",
                    "keys": [{"adapter_id": adapter_id, "version_id": version_id}],
                },
                actor={"kind": "administrator"},
                claim_epoch=1,
                child_operations={str(guard_id): {"generation": 1}},
                result={
                    "deleted": 1,
                    "freed_bytes": 4096,
                    "child_operations": [
                        {
                            "guard_operation_id": str(guard_id),
                            "generation": 1,
                            "adapter_id": adapter_id,
                            "version_id": version_id,
                            "phase": "completed",
                        }
                    ],
                },
                error_code="cache_operation_failed",
            )
        )
        session.flush()
        session.add(
            WorkerCacheManagementChild(
                management_operation_id=parent_id,
                guard_operation_id=guard_id,
                generation=1,
            )
        )

    response = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "retry",
            "idempotency_key": "resolved-parent-retry",
            "management_operation_id": str(parent_id),
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "cache_retry_target_resolved"


def test_failed_retry_claim_resolves_original_clean_and_child_generation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "retry-chain")
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    parent_id = uuid.uuid4()
    guard_id = uuid.uuid4()
    clean_payload = {
        "kind": "clean",
        "keys": [{"adapter_id": adapter_id, "version_id": version_id}],
    }
    with session_factory.begin() as session:
        session.add(
            WorkerCacheOperation(
                operation_id=guard_id,
                worker_id=worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                generation=3,
                phase="acquired",
                operation_kind="gc",
            )
        )
        parent = WorkerCacheManagementOperation(
            operation_id=parent_id,
            worker_id=worker_id,
            kind="clean",
            status="failed",
            idempotency_key="retry-chain-parent",
            request_hash="a" * 64,
            request_payload=clean_payload,
            actor={"kind": "administrator"},
            claim_epoch=1,
            child_operations={str(guard_id): {"generation": 3}},
            result={},
            error_code="cache_operation_failed",
        )
        session.add(parent)
        session.flush()
        session.add(
            WorkerCacheManagementChild(
                management_operation_id=parent_id,
                guard_operation_id=guard_id,
                generation=3,
            )
        )

    first_created = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "retry",
            "idempotency_key": "retry-chain-r1",
            "management_operation_id": str(parent_id),
        },
    )
    assert first_created.status_code == 202, first_created.text
    first_claim = api_client.post(
        f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS
    )
    assert first_claim.status_code == 200, first_claim.text
    first_command = first_claim.json()
    child = {
        "guard_operation_id": str(guard_id),
        "generation": 3,
        "adapter_id": adapter_id,
        "version_id": version_id,
        "phase": "failed",
    }
    failed = api_client.post(
        f"/api/workers/{worker_id}/cache/commands/{first_command['operation_id']}/result",
        headers=WORKER_HEADERS,
        json={
            "claim_epoch": first_command["claim_epoch"],
            "request_hash": first_command["request_hash"],
            "status": "failed",
            "result": {"child_operations": [child], "deleted": 0, "freed_bytes": 0},
            "error_code": "cache_operation_failed",
        },
    )
    assert failed.status_code == 204, failed.text
    first_retry_id = uuid.UUID(first_command["operation_id"])
    with session_factory() as session:
        assert session.get(WorkerCacheManagementChild, (first_retry_id, guard_id)) is not None

    second_created = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={
            "kind": "retry",
            "idempotency_key": "retry-chain-r2",
            "management_operation_id": str(first_retry_id),
        },
    )
    assert second_created.status_code == 202, second_created.text
    second_claim = api_client.post(
        f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS
    )
    assert second_claim.status_code == 200, second_claim.text
    payload = second_claim.json()["payload"]
    assert payload["retry_command"]["kind"] == "clean"
    assert payload["retry_command"]["payload"] == clean_payload
    assert payload["retry_root_operation_id"] == str(parent_id)
    assert payload["retry_children"] == [{"guard_operation_id": str(guard_id), "generation": 3}]


def test_mixed_children_second_retry_succeeds_and_counts_actuals_once(tmp_path: Path) -> None:
    cache, lifecycle, manager = policy_manager(
        tmp_path,
        replace(
            CachePolicy(),
            max_bytes=1_048_576,
            disk_reserve_bytes=0,
            min_idle_seconds=0,
        ),
    )
    for version_id in (13, 14):
        identity, digest = policy_ready(
            cache, lifecycle, version_id=version_id, payload=b"x" * 4096
        )
        lifecycle.confirm_rebuildability(
            f"11-{version_id}",
            identity=identity,
            digest=digest,
            source_policy="verified_offline",
            evidence_note="owned",
            actor="admin",
            valid_until=datetime.now(UTC).timestamp() + 300,
        )
    guard_client = _SecondKeyUnavailable()
    guard_client.fail_checks = True
    manager.deletion.client = guard_client
    manager.deletion.retry_seconds = 0
    agent = _local_agent(lifecycle, manager)
    parent_id = uuid.uuid4()
    clean_payload = {
        "keys": [{"adapter_id": 11, "version_id": 13}, {"adapter_id": 11, "version_id": 14}]
    }
    parent = {
        "operation_id": str(parent_id),
        "claim_epoch": 1,
        "request_hash": "c" * 64,
        "kind": "clean",
        "actor": "admin",
        "payload": clean_payload,
    }
    assert agent._execute_cache_command(7, parent)
    first = agent._client.reports[-1]
    assert first["status"] == "failed"
    assert first["result"]["deleted"] == 1
    children = first["result"]["child_operations"]

    first_retry_id = uuid.uuid4()
    first_retry = {
        "operation_id": str(first_retry_id),
        "claim_epoch": 1,
        "request_hash": "d" * 64,
        "kind": "retry",
        "actor": "admin",
        "payload": {
            "management_operation_id": str(parent_id),
            "retry_root_operation_id": str(parent_id),
            "retry_command": {"kind": "clean", "payload": clean_payload},
            "retry_children": children,
        },
    }
    assert agent._execute_cache_command(7, first_retry)
    failed_retry = agent._client.reports[-1]
    assert failed_retry["status"] == "failed"
    assert failed_retry["result"]["deleted"] == 1

    guard_client.fail_checks = False
    second_retry = {
        "operation_id": str(uuid.uuid4()),
        "claim_epoch": 1,
        "request_hash": "e" * 64,
        "kind": "retry",
        "actor": "admin",
        "payload": {
            "management_operation_id": str(first_retry_id),
            "retry_root_operation_id": str(parent_id),
            "retry_command": {"kind": "clean", "payload": clean_payload},
            "retry_children": failed_retry["result"]["child_operations"],
        },
    }
    assert agent._execute_cache_command(7, second_retry)
    completed = agent._client.reports[-1]
    assert completed["status"] == "completed"
    assert completed["result"]["deleted"] == 2
    assert completed["result"]["freed_bytes"] >= 8192
    assert not cache.entry_path("11-13").exists()
    assert not cache.entry_path("11-14").exists()


def test_cleanup_retry_reuses_request_and_one_failure_stops(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, _version_id = _fixture(
        api_client, session_factory, monkeypatch, "cleanup-retry"
    )
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    deleted = api_client.delete(f"/api/adapters/{adapter_id}")
    assert deleted.status_code == 204
    with session_factory.begin() as session:
        cleanup = session.scalar(
            select(WorkerCleanupRequest).where(WorkerCleanupRequest.adapter_id == adapter_id)
        )
        assert cleanup is not None
        cleanup.status = "failed"
        cleanup.attempts = 3
        cleanup_id = cleanup.id
    created = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={"kind": "retry", "idempotency_key": "cleanup-r1", "cleanup_id": cleanup_id},
    )
    assert created.status_code == 202, created.text
    task = api_client.post(f"/api/workers/{worker_id}/cleanups/claim", headers=WORKER_HEADERS)
    assert task.status_code == 200
    assert task.json()["cleanup_id"] == cleanup_id
    assert task.json()["claim_attempt"] == 4
    failed = api_client.post(
        f"/api/workers/{worker_id}/cleanups/{cleanup_id}/result",
        headers=WORKER_HEADERS,
        json={
            "success": False,
            "claim_attempt": 4,
            "error_code": "cache_cleanup_failed",
        },
    )
    assert failed.status_code == 204
    with session_factory() as session:
        cleanup = session.get(WorkerCleanupRequest, cleanup_id)
        operation = session.get(
            WorkerCacheManagementOperation, uuid.UUID(created.json()["operation_id"])
        )
        assert cleanup is not None and cleanup.status == "failed" and cleanup.attempts == 4
        assert operation is not None and operation.status == "failed"
    assert (
        api_client.post(
            f"/api/workers/{worker_id}/cleanups/claim", headers=WORKER_HEADERS
        ).status_code
        == 204
    )
    retried = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={"kind": "retry", "idempotency_key": "cleanup-r2", "cleanup_id": cleanup_id},
    )
    assert retried.status_code == 202, retried.text
    fifth = api_client.post(f"/api/workers/{worker_id}/cleanups/claim", headers=WORKER_HEADERS)
    assert fifth.status_code == 200 and fifth.json()["claim_attempt"] == 5
    assert (
        api_client.post(
            f"/api/workers/{worker_id}/cleanups/{cleanup_id}/result",
            headers=WORKER_HEADERS,
            json={"success": True, "claim_attempt": 4},
        ).status_code
        == 409
    )
    assert (
        api_client.post(
            f"/api/workers/{worker_id}/cleanups/{cleanup_id}/result",
            headers=WORKER_HEADERS,
            json={"success": True, "claim_attempt": 5},
        ).status_code
        == 204
    )


def test_cleanup_retry_and_registration_share_worker_first_lock_order(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, _version_id = _fixture(
        api_client, session_factory, monkeypatch, "cleanup-lock-order"
    )
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    assert api_client.delete(f"/api/adapters/{adapter_id}").status_code == 204
    with session_factory.begin() as session:
        cleanup = session.scalar(
            select(WorkerCleanupRequest).where(WorkerCleanupRequest.adapter_id == adapter_id)
        )
        assert cleanup is not None
        cleanup.status = "failed"
        cleanup.attempts = 3
        cleanup_id = cleanup.id
    retry = api_client.post(
        f"/api/workers/{worker_id}/cache/operations",
        json={"kind": "retry", "idempotency_key": "cleanup-lock", "cleanup_id": cleanup_id},
    )
    assert retry.status_code == 202
    claimed = api_client.post(f"/api/workers/{worker_id}/cleanups/claim", headers=WORKER_HEADERS)
    assert claimed.status_code == 200 and claimed.json()["claim_attempt"] == 4

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, int]] = []

    def register() -> None:
        payload = ready_registration(str(worker["name"]), ["python"])
        payload["isolation_capabilities"]["cache_governance_v1"] = True
        barrier.wait()
        response = api_client.post("/api/workers/register", json=payload, headers=WORKER_HEADERS)
        outcomes.append(("register", response.status_code))

    def finish() -> None:
        barrier.wait()
        response = api_client.post(
            f"/api/workers/{worker_id}/cleanups/{cleanup_id}/result",
            headers=WORKER_HEADERS,
            json={"success": True, "claim_attempt": 4},
        )
        outcomes.append(("finish", response.status_code))

    threads = [threading.Thread(target=register), threading.Thread(target=finish)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert ("register", 200) in outcomes
    assert next(status for name, status in outcomes if name == "finish") in {204, 409}

    with session_factory() as session:
        cleanup = session.get(WorkerCleanupRequest, cleanup_id)
        assert cleanup is not None
        if cleanup.status == "pending":
            assert cleanup.attempts == 4
        else:
            assert cleanup.status == "completed"


def test_retention_is_bounded_and_preserves_unfinished_cleanup_link(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    worker, adapter, _version_id = _fixture(api_client, session_factory, monkeypatch, "retention")
    worker_id = int(worker["id"])
    adapter_id = int(adapter["id"])
    assert api_client.delete(f"/api/adapters/{adapter_id}").status_code == 204
    protected_id, newest_id, oldest_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with session_factory.begin() as session:
        for operation_id, key, age in (
            (protected_id, "protected", 3),
            (newest_id, "newest", 1),
            (oldest_id, "oldest", 2),
        ):
            session.add(
                WorkerCacheManagementOperation(
                    operation_id=operation_id,
                    worker_id=worker_id,
                    kind="retry" if operation_id == protected_id else "preview",
                    status="failed" if operation_id == protected_id else "completed",
                    idempotency_key=key,
                    request_hash=(key[0] * 64),
                    request_payload={"kind": "preview"},
                    actor={"kind": "superadmin"},
                    claim_epoch=1,
                    target_kind="cleanup" if operation_id == protected_id else None,
                    target_cleanup_id=None,
                    created_at=datetime.now(UTC) - timedelta(days=age),
                    finished_at=datetime.now(UTC) - timedelta(days=age),
                )
            )
        session.flush()
        cleanup = session.scalar(
            select(WorkerCleanupRequest).where(WorkerCleanupRequest.adapter_id == adapter_id)
        )
        assert cleanup is not None
        cleanup.status = "failed"
        cleanup.retry_operation_id = protected_id
        protected = session.get(WorkerCacheManagementOperation, protected_id)
        assert protected is not None
        protected.target_cleanup_id = cleanup.id
    monkeypatch.setattr(cache_admin_service, "MAX_TERMINAL_PER_WORKER", 1)
    with session_factory() as session:
        assert cache_admin_service.prune_terminal(session, worker_id) == 1
    with session_factory() as session:
        assert session.get(WorkerCacheManagementOperation, protected_id) is not None
        assert session.get(WorkerCacheManagementOperation, newest_id) is not None
        assert session.get(WorkerCacheManagementOperation, oldest_id) is None


def test_local_command_receipt_replays_after_restart_without_repeating_side_effect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lifecycle = CacheLifecycleStore.for_runtime(tmp_path)
    lifecycle.bind_owner(7)
    client = _CommandClient()
    operation_id = uuid.uuid4()
    command = {
        "operation_id": str(operation_id),
        "claim_epoch": 1,
        "request_hash": "a" * 64,
        "kind": "preview",
        "payload": {"kind": "preview", "keys": []},
        "actor": "superadmin",
    }

    first = Agent.__new__(Agent)
    first._cache_lifecycle = lifecycle
    first._cache_deletion_manager = None
    first._client = client
    calls = 0

    def execute_once(*_args, **_kwargs) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"complete": True, "items": []}

    monkeypatch.setattr(first, "_run_cache_command", execute_once)
    assert first._execute_cache_command(7, command)
    assert calls == 1

    restarted = Agent.__new__(Agent)
    restarted._cache_lifecycle = lifecycle
    restarted._cache_deletion_manager = None
    restarted._client = client
    monkeypatch.setattr(
        restarted,
        "_run_cache_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("side effect repeated")),
    )
    command["claim_epoch"] = 2
    assert restarted._execute_cache_command(7, command)
    assert [report["claim_epoch"] for report in client.reports] == [1, 2]
