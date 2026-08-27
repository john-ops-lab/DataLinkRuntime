"""Issue #127 C0 red/green contract tests.

The first four tests are the minimum C0 risk gate.  They intentionally cover
the boundaries that the B3 hook could not prove: a pending file Execution's
Lease, protocol gating, claim-token authentication, and terminal Lease
release.
"""

from __future__ import annotations

import base64
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings, validate_deployment_configuration
from dlr.control.models import (
    Adapter,
    AdapterInputArtifactBinding,
    Execution,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputCapacity,
    ManagedInputUploadReservation,
)
from dlr.control.schemas.execution import ExecutionCreate
from dlr.control.services import execution as execution_service
from dlr.control.services import input_config as input_config_service
from dlr.control.services import managed_input_gc
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.worker_protocol import hash_token
from test_adapters import create_adapter, save_version

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}
FIXED_NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def _register_worker(
    client: TestClient,
    name: str,
    *,
    protocol_version: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "capabilities": ["python"]}
    if protocol_version is not None:
        payload["protocol_version"] = protocol_version
    response = client.post("/api/workers/register", json=payload, headers=WORKER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _create_staged_artifact(
    session_factory: sessionmaker[Session], adapter_id: int, filename: str
) -> int:
    with session_factory.begin() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=adapter_id,
            upload_session_id=f"c0-session-{adapter_id}-{filename}",
            reserved_bytes=0,
            status="CONSUMED",
            expires_at=FIXED_NOW + timedelta(days=1),
            consumed_at=FIXED_NOW,
        )
        session.add(reservation)
        session.flush()
        artifact = ManagedInputArtifact(
            adapter_id=adapter_id,
            created_by_user_id=None,
            upload_session_id=reservation.upload_session_id,
            upload_reservation_id=reservation.id,
            original_filename=filename,
            storage_key=f"{adapter_id:016x}{reservation.id:048x}",
            content_type="text/plain",
            size_bytes=8,
            sha256="a" * 64,
            status="STAGED",
            retention_mode="system_default",
            expires_at=FIXED_NOW + timedelta(hours=1),
            created_at=FIXED_NOW,
        )
        session.add(artifact)
        session.flush()
        return int(artifact.id)


def _materialize_blob(
    session_factory: sessionmaker[Session], artifact_id: int, store: LocalFileArtifactStore
) -> str:
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        storage_key = artifact.storage_key
    with store.put_part(storage_key) as part:
        part.write(b"payload!")
    store.commit(storage_key)
    return storage_key


def _bind_artifact(client: TestClient, adapter_id: int, artifact_id: int) -> dict[str, Any]:
    return _bind_artifacts(client, adapter_id, [artifact_id])


def _bind_artifacts(client: TestClient, adapter_id: int, artifact_ids: list[int]) -> dict[str, Any]:
    response = client.put(
        f"/api/adapters/{adapter_id}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": artifact_ids,
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _claim(client: TestClient, worker_id: int) -> Any:
    return client.post(
        f"/api/workers/{worker_id}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )


def test_c0_red_pending_file_lease_survives_configuration_replacement(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    """A lifecycle replacement must not make a pending file input deletable."""
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_adapter(api_client, name="c0-red-pending-lease")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "pending.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text

    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.expires_at = FIXED_NOW - timedelta(seconds=1)
    with session_factory() as session:
        input_config_service.reconcile_current_bindings(session, adapter["id"], now=FIXED_NOW)
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is True


def test_c0_red_v1_worker_cannot_claim_managed_files(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-red-v1-worker")
    adapter = create_adapter(api_client, name="c0-red-v1-files")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "v1.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text

    response = _claim(api_client, worker["id"])
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "worker_protocol_incompatible"


def test_c0_red_v2_result_without_claim_token_is_rejected(api_client: TestClient) -> None:
    worker = _register_worker(api_client, "c0-red-no-token-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-red-no-token")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text

    response = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}/result",
        json={"status": "succeeded"},
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "execution_claim_token_invalid"


def test_c0_red_terminal_execution_releases_file_lease(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-red-terminal-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-red-terminal-lease")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "terminal.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text

    response = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}/result",
        json={"status": "succeeded"},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert response.status_code == 200, response.text
    with session_factory() as session:
        assert (
            session.scalar(
                select(AdapterInputArtifactBinding).where(
                    AdapterInputArtifactBinding.artifact_id == artifact_id
                )
            )
            is not None
        )
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is False


def test_c0_execution_snapshot_and_v2_claim_credentials_are_immutable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "c0-snapshot-v2-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-snapshot-v2", timeout_seconds=17)
    save_version(api_client, adapter["id"])
    execution_response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution_response.status_code == 202, execution_response.text
    execution = execution_response.json()

    assert execution["timeout_seconds_snapshot"] == 17
    assert execution["recovery_grace_seconds_snapshot"] == 60
    assert execution["workspace_cleanup_attempt_timeout_seconds_snapshot"] == 5
    assert execution["workspace_cleanup_total_timeout_seconds_snapshot"] == 20
    assert execution["claim_deadline_at"] is not None
    assert execution["workspace_cleanup_status"] is None
    assert "claim_token_hash" not in execution_response.text
    assert "cleanup_receipt_token_hash" not in execution_response.text

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    payload = claimed.json()
    assert payload["protocol_version"] == 2
    claim_token = payload["claim_token"]
    cleanup_token = payload["cleanup_token"]
    assert claim_token != cleanup_token
    assert len(base64.urlsafe_b64decode(claim_token + "==")) == 32
    assert len(base64.urlsafe_b64decode(cleanup_token + "==")) == 32
    assert payload["execution_deadline_at"] is not None
    assert payload["input_files"] == []

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        assert row.claim_token_hash == hash_token(claim_token)
        assert row.cleanup_receipt_token_hash == hash_token(cleanup_token)
        assert row.claim_token_hash != claim_token
        assert row.cleanup_receipt_token_hash != cleanup_token
        assert row.execution_deadline_at is not None


def test_c0_v2_managed_files_claim_exposes_ordered_input_file_metadata(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-managed-files-v2-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-managed-files-v2")
    save_version(api_client, adapter["id"])
    first_id = _create_staged_artifact(session_factory, adapter["id"], "first.txt")
    second_id = _create_staged_artifact(session_factory, adapter["id"], "second.txt")
    _bind_artifacts(api_client, adapter["id"], [second_id, first_id])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    payload = claimed.json()
    assert payload["input"] is None
    assert payload["input_files"] == [
        {
            "id": second_id,
            "ordinal": 0,
            "mount_name": "input-00",
            "original_filename": "second.txt",
            "content_type": "text/plain",
            "size_bytes": 8,
            "sha256": "a" * 64,
        },
        {
            "id": first_id,
            "ordinal": 1,
            "mount_name": "input-01",
            "original_filename": "first.txt",
            "content_type": "text/plain",
            "size_bytes": 8,
            "sha256": "a" * 64,
        },
    ]


def test_c0_task_payload_preserves_nullable_artifact_sha256(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy/corrupt metadata row keeps missing SHA-256 distinguishable."""
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-nullable-sha-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-nullable-sha")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "nullable.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text

    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.sha256 = None

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["input_files"][0]["sha256"] is None


def test_c0_v2_claim_without_historical_lease_stays_pending(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing pre-C0 Lease cannot publish running state or Token hashes."""
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-missing-lease-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-missing-lease")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "missing-lease.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    execution_id = execution.json()["id"]

    with session_factory.begin() as session:
        removed = (
            session.query(ExecutionInputArtifactLease).filter_by(execution_id=execution_id).delete()
        )
        assert removed == 1

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 409, claimed.text
    assert claimed.json()["detail"]["code"] == "execution_input_lease_unavailable"
    with session_factory() as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        assert row.status == "pending"
        assert row.worker_id is None
        assert row.claim_token_hash is None
        assert row.cleanup_receipt_token_hash is None


def test_c0_malformed_managed_files_row_does_not_block_later_valid_claim(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed historical row stays pending while a later row is claimable."""
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-malformed-first-worker", protocol_version=2)

    malformed_adapter = create_adapter(api_client, name="c0-malformed-first")
    save_version(api_client, malformed_adapter["id"])
    malformed_artifact_id = _create_staged_artifact(
        session_factory, malformed_adapter["id"], "malformed-first.txt"
    )
    _bind_artifact(api_client, malformed_adapter["id"], malformed_artifact_id)
    malformed = api_client.post(f"/api/adapters/{malformed_adapter['id']}/executions", json={})
    assert malformed.status_code == 202, malformed.text
    malformed_id = malformed.json()["id"]

    with session_factory.begin() as session:
        removed = (
            session.query(ExecutionInputArtifactLease).filter_by(execution_id=malformed_id).delete()
        )
        assert removed == 1

    valid_adapter = create_adapter(api_client, name="c0-valid-second")
    save_version(api_client, valid_adapter["id"])
    valid = api_client.post(f"/api/adapters/{valid_adapter['id']}/executions", json={})
    assert valid.status_code == 202, valid.text
    valid_id = valid.json()["id"]

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["execution_id"] == valid_id
    assert claimed.json()["input_files"] == []

    with session_factory() as session:
        malformed_row = session.get(Execution, malformed_id)
        valid_row = session.get(Execution, valid_id)
        assert malformed_row is not None and valid_row is not None
        assert malformed_row.status == "pending"
        assert malformed_row.worker_id is None
        assert malformed_row.claim_token_hash is None
        assert malformed_row.cleanup_receipt_token_hash is None
        assert valid_row.status == "running"
        assert valid_row.worker_id == worker["id"]


def test_c0_database_lease_provider_blocks_gc_state_blob_and_charge_release(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c0-gc-provider-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-gc-provider")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "gc-provider.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text

    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert artifact is not None and capacity is not None
        artifact.status = ManagedInputArtifactStatus.PENDING_DELETE
        capacity.actual_bytes = artifact.size_bytes

    deleted_keys: list[str] = []

    class RecordingStore:
        def delete(self, storage_key: str) -> bool:
            deleted_keys.append(storage_key)
            return True

    with session_factory() as session:
        report = managed_input_gc.process_artifact_deletions(
            session,
            store=RecordingStore(),  # type: ignore[arg-type]
            now=FIXED_NOW,
        )
        protected = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert report.deleted == 0
        assert deleted_keys == []
        assert (
            protected is not None and protected.status == ManagedInputArtifactStatus.PENDING_DELETE
        )
        assert capacity is not None and capacity.actual_bytes == 8
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is True

    result = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}/result",
        json={"status": "succeeded"},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert result.status_code == 200, result.text

    with session_factory() as session:
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is False
        report = managed_input_gc.process_artifact_deletions(
            session,
            store=RecordingStore(),  # type: ignore[arg-type]
            now=FIXED_NOW,
        )
        deleted = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert report.deleted == 1
        assert deleted is not None and deleted.status == ManagedInputArtifactStatus.DELETED
        assert capacity is not None and capacity.actual_bytes == 0
        assert deleted_keys == [artifact.storage_key]


def test_c0_v2_result_without_cleanup_report_is_deferred_unknown(
    api_client: TestClient,
) -> None:
    worker = _register_worker(api_client, "c0-result-without-cleanup-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-result-without-cleanup")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text

    result = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}/result",
        json={"status": "succeeded"},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["workspace_cleanup_status"] == "deferred"
    assert result.json()["workspace_cleanup_error_code"] == "workspace_cleanup_unknown"


@pytest.mark.parametrize("field_name", ["error_code", "workspace_cleanup_error_code"])
def test_c0_result_error_code_length_is_validated_before_state_change(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    field_name: str,
) -> None:
    worker = _register_worker(
        api_client,
        f"c0-result-error-code-{field_name}",
        protocol_version=2,
    )
    adapter = create_adapter(api_client, name=f"c0-result-error-code-{field_name}")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    execution_id = execution.json()["id"]
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text

    payload = {"status": "succeeded", field_name: "x" * 65}
    result = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution_id}/result",
        json=payload,
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert result.status_code == 422, result.text

    with session_factory() as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        assert row.status == "running"
        assert row.ended_at is None
        assert row.error_code is None
        assert row.workspace_cleanup_status is None
        assert row.workspace_cleanup_error_code is None


@pytest.mark.parametrize("stale_status", ["pending", "running"])
def test_c0_red_stale_execution_lease_stays_protected_until_c3_reconciler(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    stale_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C0 keeps stale work protected; C3 owns the later stale transition.

    Before C0, the B3 hook returned ``False`` for both stale states, so GC
    could delete their Blob and release charge while the Execution still
    referenced it.  C0's durable provider must instead keep the Lease until
    C3's 13.1/13.2 reconciler atomically writes a terminal state and releases
    it; this test intentionally does not implement or invoke that reconciler.
    """
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(
        api_client,
        f"c0-stale-{stale_status}-worker",
        protocol_version=2,
    )
    adapter = create_adapter(api_client, name=f"c0-stale-{stale_status}")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(
        session_factory, adapter["id"], f"stale-{stale_status}.txt"
    )
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    execution_id = execution.json()["id"]
    if stale_status == "running":
        claimed = _claim(api_client, worker["id"])
        assert claimed.status_code == 200, claimed.text

    with session_factory.begin() as session:
        row = session.get(Execution, execution_id)
        artifact = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert row is not None and artifact is not None and capacity is not None
        row.claim_deadline_at = FIXED_NOW - timedelta(minutes=5)
        if stale_status == "running":
            row.execution_deadline_at = FIXED_NOW - timedelta(minutes=5)
        artifact.status = ManagedInputArtifactStatus.PENDING_DELETE
        capacity.actual_bytes = artifact.size_bytes

    deleted_keys: list[str] = []

    class RecordingStore:
        def delete(self, storage_key: str) -> bool:
            deleted_keys.append(storage_key)
            return True

    with session_factory() as session:
        report = managed_input_gc.process_artifact_deletions(
            session,
            store=RecordingStore(),  # type: ignore[arg-type]
            now=FIXED_NOW,
        )
        protected = session.get(ManagedInputArtifact, artifact_id)
        current = session.get(Execution, execution_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert report.deleted == 0
        assert deleted_keys == []
        assert (
            protected is not None and protected.status == ManagedInputArtifactStatus.PENDING_DELETE
        )
        assert current is not None and current.status == stale_status and current.ended_at is None
        assert capacity is not None and capacity.actual_bytes == 8
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is True


def test_c0_postgres_lock_order_lease_first_blocks_gc_without_blob_or_charge_change(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Two PostgreSQL transactions serialize Lease creation before GC."""
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_adapter(api_client, name="c0-pg-lease-first")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "lease-first.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    store = LocalFileArtifactStore(tmp_path / "lease-first-store")
    storage_key = _materialize_blob(session_factory, artifact_id, store)
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert artifact is not None and capacity is not None
        capacity.actual_bytes = artifact.size_bytes

    creator_reached_artifact = threading.Event()
    lock_barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    errors: list[tuple[str, BaseException]] = []
    real_database_now = input_config_service.database_now

    def pause_after_artifact_lock(session: Session) -> Any:
        if (
            threading.current_thread().name == "c0-lease-first-creator"
            and not creator_reached_artifact.is_set()
        ):
            creator_reached_artifact.set()
            lock_barrier.wait(timeout=5)
        return real_database_now(session)

    monkeypatch.setattr(input_config_service, "database_now", pause_after_artifact_lock)

    def creator() -> None:
        try:
            with session_factory() as session:
                created = execution_service.create_execution(
                    session,
                    adapter["id"],
                    ExecutionCreate(),
                )
                outcomes["execution_id"] = created.id
        except BaseException as exc:  # pragma: no cover - failure handoff
            errors.append(("creator", exc))
            lock_barrier.abort()

    def gc_competitor() -> None:
        try:
            with session_factory() as session:
                if not creator_reached_artifact.wait(timeout=5):
                    raise AssertionError("creator did not reach the Artifact lock")
                lock_barrier.wait(timeout=5)
                # This is the blocking row-lock section of the GC transaction.
                # It can return only after the creator commits its Execution and
                # Lease, because both paths lock Artifact before the decision.
                observed = session.scalar(
                    select(ManagedInputArtifact)
                    .where(ManagedInputArtifact.id == artifact_id)
                    .with_for_update()
                )
                assert observed is not None
                assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is True
                outcomes["gc_observed_lease"] = True
                # Make the already-governed row a deletion candidate, then use
                # the actual C0 provider/GC claim path for the safety assertion.
                observed.status = ManagedInputArtifactStatus.PENDING_DELETE
                session.commit()
                report = managed_input_gc.process_artifact_deletions(
                    session,
                    store=store,
                    now=FIXED_NOW,
                )
                outcomes["gc_report"] = report
        except BaseException as exc:  # pragma: no cover - failure handoff
            errors.append(("gc", exc))
            lock_barrier.abort()

    creator_thread = threading.Thread(target=creator, name="c0-lease-first-creator")
    gc_thread = threading.Thread(target=gc_competitor, name="c0-lease-first-gc")
    creator_thread.start()
    gc_thread.start()
    creator_thread.join(timeout=10)
    gc_thread.join(timeout=10)
    assert not creator_thread.is_alive() and not gc_thread.is_alive()
    assert errors == [], repr(errors)
    assert outcomes.get("execution_id") is not None
    assert outcomes.get("gc_observed_lease") is True
    report = outcomes["gc_report"]
    assert report.claimed == 0 and report.deleted == 0

    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        execution = session.get(Execution, outcomes["execution_id"])
        assert artifact is not None and capacity is not None and execution is not None
        assert execution.status == "pending"
        assert artifact.status == ManagedInputArtifactStatus.PENDING_DELETE
        assert capacity.actual_bytes == 8
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is True
        assert store.stat(storage_key) is not None


def test_c0_postgres_lock_order_governance_first_rejects_creation_after_blob_delete(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed GC claim makes the later managed-file create fail safely."""
    from dlr.control.input_errors import InputConfigErrorCode

    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_adapter(api_client, name="c0-pg-governance-first")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "governance-first.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    store = LocalFileArtifactStore(tmp_path / "governance-first-store")
    storage_key = _materialize_blob(session_factory, artifact_id, store)
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert artifact is not None and capacity is not None
        artifact.status = ManagedInputArtifactStatus.PENDING_DELETE
        artifact.expires_at = FIXED_NOW - timedelta(seconds=1)
        capacity.actual_bytes = artifact.size_bytes

    creator_has_adapter_lock = threading.Event()
    gc_claimed = threading.Event()
    lock_barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    errors: list[tuple[str, BaseException]] = []

    def creator() -> None:
        try:
            with session_factory() as session:
                adapter_row = session.get(Adapter, adapter["id"], with_for_update=True)
                assert adapter_row is not None
                creator_has_adapter_lock.set()
                lock_barrier.wait(timeout=5)
                if not gc_claimed.wait(timeout=5):
                    raise AssertionError("GC did not commit its candidate claim")
                try:
                    execution_service.create_execution(
                        session,
                        adapter["id"],
                        ExecutionCreate(),
                    )
                except HTTPException as exc:
                    detail = exc.detail
                    assert isinstance(detail, dict)
                    outcomes["create_failure"] = {
                        "status_code": exc.status_code,
                        "code": detail["code"],
                    }
                    session.rollback()
                else:  # pragma: no cover - the assertion documents the race
                    raise AssertionError("governance-first create unexpectedly succeeded")
        except BaseException as exc:  # pragma: no cover - failure handoff
            errors.append(("creator", exc))
            lock_barrier.abort()

    def gc_competitor() -> None:
        try:
            with session_factory() as session:
                if not creator_has_adapter_lock.wait(timeout=5):
                    raise AssertionError("creator did not acquire the Adapter lock")
                lock_barrier.wait(timeout=5)
                claim = managed_input_gc.claim_artifact_deletion(
                    session,
                    artifact_id,
                    now=FIXED_NOW,
                    force=True,
                )
                assert claim is not None
                outcomes["gc_claimed"] = True
                gc_claimed.set()
                store.delete(claim.storage_key)
                assert managed_input_gc.finalize_artifact_deletion(
                    session,
                    claim,
                    succeeded=True,
                    now=FIXED_NOW,
                )
        except BaseException as exc:  # pragma: no cover - failure handoff
            errors.append(("gc", exc))
            gc_claimed.set()
            lock_barrier.abort()

    creator_thread = threading.Thread(target=creator, name="c0-governance-first-creator")
    gc_thread = threading.Thread(target=gc_competitor, name="c0-governance-first-gc")
    creator_thread.start()
    gc_thread.start()
    creator_thread.join(timeout=10)
    gc_thread.join(timeout=10)
    assert not creator_thread.is_alive() and not gc_thread.is_alive()
    assert errors == [], repr(errors)
    assert outcomes.get("gc_claimed") is True
    assert outcomes.get("create_failure") == {
        "status_code": 422,
        "code": InputConfigErrorCode.INVALID.value,
    }

    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        execution = session.scalar(
            select(Execution).where(Execution.adapter_id == adapter["id"]).limit(1)
        )
        assert artifact is not None and capacity is not None
        assert artifact.status == ManagedInputArtifactStatus.DELETED
        assert capacity.actual_bytes == 0
        assert execution is None
        assert managed_input_gc.has_active_artifact_lease(session, artifact_id) is False
        assert store.stat(storage_key) is None


def test_c0_worker_route_headers_and_non_worker_boundary(
    api_client: TestClient,
) -> None:
    path = "/api/workers/999999/executions/999999/input-artifacts/1/content"
    non_worker = api_client.get(path)
    assert non_worker.status_code == 401

    worker = _register_worker(api_client, "c0-route-v2-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-route-contract")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    execution_id = execution.json()["id"]
    artifact_path = (
        f"/api/workers/{worker['id']}/executions/{execution_id}/input-artifacts/1/content"
    )

    missing = api_client.get(artifact_path, headers=WORKER_HEADERS)
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "execution_claim_token_invalid"
    swapped = api_client.get(
        artifact_path,
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": claimed.json()["cleanup_token"]},
    )
    assert swapped.status_code == 422
    assert swapped.json()["detail"]["code"] == "execution_claim_token_invalid"
    in_url = api_client.get(
        artifact_path + "?claim_token=" + claimed.json()["claim_token"],
        headers=WORKER_HEADERS,
    )
    assert in_url.status_code == 422
    assert in_url.json()["detail"]["code"] == "execution_claim_token_invalid"
    ready = api_client.get(
        artifact_path,
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert ready.status_code == 422
    assert ready.json()["detail"]["code"] == "input_artifact_not_ready"


def test_c0_cleanup_receipt_route_keeps_cleanup_token_separate(
    api_client: TestClient,
) -> None:
    worker = _register_worker(api_client, "c0-receipt-v2-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c0-receipt-contract")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    execution_id = execution.json()["id"]
    result = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution_id}/result",
        json={"status": "succeeded", "workspace_cleanup_status": "deferred"},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert result.status_code == 200, result.text
    receipt_path = f"/api/workers/{worker['id']}/executions/{execution_id}/cleanup-receipt"
    missing = api_client.post(receipt_path, json={"status": "completed"}, headers=WORKER_HEADERS)
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "execution_cleanup_token_invalid"
    swapped = api_client.post(
        receipt_path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert swapped.status_code == 422
    assert swapped.json()["detail"]["code"] == "execution_cleanup_token_invalid"
    accepted = api_client.post(
        receipt_path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": claimed.json()["cleanup_token"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["workspace_cleanup_status"] == "completed"


def test_c0_cleanup_budget_invariant_and_existing_execution_snapshot(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: Any,
) -> None:
    _register_worker(api_client, "c0-budget-worker")
    adapter = create_adapter(api_client, name="c0-budget-snapshot")
    save_version(api_client, adapter["id"])
    first = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert first.status_code == 202, first.text
    first_body = first.json()
    assert api_client.post(f"/api/executions/{first_body['id']}/cancel").status_code == 200

    monkeypatch.setattr(settings, "execution_recovery_grace_seconds", 45)
    monkeypatch.setattr(settings, "workspace_cleanup_attempt_timeout_seconds", 4)
    monkeypatch.setattr(settings, "workspace_cleanup_total_timeout_seconds", 15)
    validate_deployment_configuration(settings)
    second = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert second.status_code == 202, second.text
    assert second.json()["recovery_grace_seconds_snapshot"] == 45
    assert second.json()["workspace_cleanup_attempt_timeout_seconds_snapshot"] == 4
    assert second.json()["workspace_cleanup_total_timeout_seconds_snapshot"] == 15

    with session_factory() as session:
        historical = session.get(Execution, first_body["id"])
        current = session.get(Execution, second.json()["id"])
        assert historical is not None and current is not None
        assert historical.recovery_grace_seconds_snapshot == 60
        assert current.recovery_grace_seconds_snapshot == 45
        assert (
            session.scalar(
                select(ExecutionInputArtifactLease.execution_id).where(
                    ExecutionInputArtifactLease.execution_id == first_body["id"]
                )
            )
            is None
        )
