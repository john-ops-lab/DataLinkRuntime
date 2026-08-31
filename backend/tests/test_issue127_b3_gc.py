"""Issue #127 B3 TTL, GC, deletion-job and audit contract tests."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.input_errors import ManagedInputErrorCode
from dlr.control.models import (
    Adapter,
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    ArtifactDeletionJob,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputCapacity,
    ManagedInputDeletionJobStatus,
    ManagedInputUploadReservation,
)
from dlr.control.services import managed_input_gc
from dlr.control.services.artifact_store import ArtifactStoreError, LocalFileArtifactStore


def create_task(api_client: TestClient, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_artifact(
    session: Session,
    adapter_id: int,
    store: LocalFileArtifactStore,
    *,
    status: str = ManagedInputArtifactStatus.STAGED,
    size_bytes: int = 3,
    expires_at: datetime | None = None,
) -> ManagedInputArtifact:
    reservation_status = "ACTIVE" if status == ManagedInputArtifactStatus.UPLOADING else "CONSUMED"
    reservation = ManagedInputUploadReservation(
        adapter_id=adapter_id,
        upload_session_id=f"b3-session-{os.urandom(8).hex()}",
        reserved_bytes=size_bytes,
        status=reservation_status,
        expires_at=expires_at or datetime.now(UTC) + timedelta(hours=1),
    )
    session.add(reservation)
    session.flush()
    key = store.new_storage_key()
    with store.put_part(key) as part:
        part.write(b"abc")
    store.commit(key)
    artifact = ManagedInputArtifact(
        adapter_id=adapter_id,
        upload_session_id=reservation.upload_session_id,
        upload_reservation_id=reservation.id,
        original_filename="b3.txt",
        storage_key=key,
        content_type="text/plain",
        size_bytes=size_bytes,
        status=status,
        expires_at=expires_at,
    )
    session.add(artifact)
    if status == ManagedInputArtifactStatus.UPLOADING:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        capacity.reserved_bytes += size_bytes
    else:
        reservation.status = "CONSUMED"
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        capacity.actual_bytes += size_bytes
    session.flush()
    return artifact


def test_stale_deleting_artifact_is_reclaimed_when_object_is_missing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-stale-deleting")
    store = LocalFileArtifactStore(tmp_path / "store")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.DELETING,
            size_bytes=3,
        )
        artifact.delete_lease_until = now - timedelta(seconds=1)
        artifact.delete_started_at = now - timedelta(minutes=5)

    with session_factory() as session:
        store.delete(artifact.storage_key)
        report = managed_input_gc.run_gc_cycle(
            session,
            store=store,
            now=now,
            protection_hook=lambda _session, _artifact_id: False,
        )
        assert report.artifacts_deleted == 1

    with session_factory() as session:
        refreshed = session.get(ManagedInputArtifact, artifact.id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert refreshed is not None and refreshed.status == ManagedInputArtifactStatus.DELETED
        assert capacity is not None and capacity.actual_bytes == 0


def test_fresh_staged_artifact_is_not_swept_before_its_ttl(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-fresh-staged")
    store = LocalFileArtifactStore(tmp_path / "store")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.STAGED,
            size_bytes=3,
            expires_at=now + timedelta(hours=1),
        )

    with session_factory() as session:
        report = managed_input_gc.run_gc_cycle(session, store=store, now=now)
        fresh = session.get(ManagedInputArtifact, artifact.id)
        assert report.staged_marked == 0
        assert fresh is not None and fresh.status == ManagedInputArtifactStatus.STAGED
        assert store.stat(artifact.storage_key) is not None


def test_startup_gc_does_not_sweep_fresh_staged_artifact(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The immediate lifespan tick must still honor the STAGED expiry."""
    adapter = create_task(api_client, "b3-startup-fresh-staged")
    store_root = tmp_path / "startup-store"
    store = LocalFileArtifactStore(store_root)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.STAGED,
            size_bytes=3,
            expires_at=now + timedelta(hours=1),
        )

    from dlr.common.config import settings

    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "artifact_gc_interval_seconds", 3_600.0)
    monkeypatch.setattr(settings, "artifact_audit_interval_seconds", 3_600.0)
    with TestClient(api_client.app):
        time.sleep(0.2)

    with session_factory() as session:
        fresh = session.get(ManagedInputArtifact, artifact.id)
        assert fresh is not None and fresh.status == ManagedInputArtifactStatus.STAGED
        assert store.stat(artifact.storage_key) is not None


def test_protected_delete_hook_blocks_gc(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-protected-hook")
    store = LocalFileArtifactStore(tmp_path / "store")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.PENDING_DELETE,
            size_bytes=3,
        )

    protected_ids: list[int] = []

    def protected_hook(_session: Session, artifact_id: int) -> bool:
        protected_ids.append(artifact_id)
        return True

    with session_factory() as session:
        report = managed_input_gc.run_gc_cycle(
            session,
            store=store,
            now=now,
            protection_hook=protected_hook,
        )
        protected = session.get(ManagedInputArtifact, artifact.id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert report.artifacts_deleted == 0
        assert protected_ids == [artifact.id]
        assert (
            protected is not None and protected.status == ManagedInputArtifactStatus.PENDING_DELETE
        )
        assert capacity is not None and capacity.actual_bytes == 3
        assert store.stat(artifact.storage_key) is not None


def test_manual_delete_reports_live_deleting_claim(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-delete-in-progress")
    store = LocalFileArtifactStore(tmp_path / "store")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.DELETING,
            size_bytes=3,
        )
        artifact.delete_attempts = 1
        artifact.delete_started_at = now
        artifact.delete_lease_until = now + timedelta(minutes=5)
        artifact_id = int(artifact.id)
        storage_key = artifact.storage_key

    response = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert response.status_code == 409, response.text
    assert (
        response.json()["detail"]["code"] == ManagedInputErrorCode.ARTIFACT_DELETE_IN_PROGRESS.value
    )
    with session_factory() as session:
        current = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert current is not None and current.status == ManagedInputArtifactStatus.DELETING
        assert capacity is not None and capacity.actual_bytes == 3
    assert store.stat(storage_key) is not None


def test_manual_delete_is_idempotent_for_missing_or_cross_adapter_artifact(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-missing-delete")
    other_adapter = create_task(api_client, "b3-cross-adapter-delete")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(session, other_adapter["id"], store, size_bytes=3)
        artifact_id = int(artifact.id)
        storage_key = artifact.storage_key

    missing = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/999999")
    cross_adapter = api_client.delete(
        f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}"
    )
    assert missing.status_code == 204, missing.text
    assert cross_adapter.status_code == 204, cross_adapter.text
    with session_factory() as session:
        current = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert current is not None and current.adapter_id == other_adapter["id"]
        assert current.status == ManagedInputArtifactStatus.STAGED
        assert capacity is not None and capacity.actual_bytes == 3
    assert store.stat(storage_key) is not None


def test_manual_delete_is_idempotent_for_repeated_request(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b3-repeat-delete")
    store = LocalFileArtifactStore(tmp_path / "store")
    from dlr.common.config import settings

    monkeypatch.setattr(settings, "artifact_store_root", str(store.root))
    with session_factory.begin() as session:
        artifact = create_artifact(session, adapter["id"], store, size_bytes=3)
        artifact_id = int(artifact.id)
        storage_key = artifact.storage_key

    first = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    second = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    with session_factory() as session:
        deleted = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert deleted is not None and deleted.status == ManagedInputArtifactStatus.DELETED
        assert capacity is not None and capacity.actual_bytes == 0
    assert store.stat(storage_key) is None


def test_manual_delete_is_idempotent_when_adapter_delete_wins_race(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b3-adapter-delete-race")
    store = LocalFileArtifactStore(tmp_path / "store")
    from dlr.common.config import settings

    monkeypatch.setattr(settings, "artifact_store_root", str(store.root))
    with session_factory.begin() as session:
        artifact = create_artifact(session, adapter["id"], store, size_bytes=3)
        artifact_id = int(artifact.id)
        storage_key = artifact.storage_key

    from dlr.control.services import adapter as adapter_service

    def adapter_delete_wins(
        session: Session,
        _artifact_id: int,
        *,
        adapter_id: int | None = None,
        **_kwargs: object,
    ) -> None:
        assert adapter_id == adapter["id"]
        adapter_service.delete_adapter(session, int(adapter_id))
        return None

    monkeypatch.setattr(managed_input_gc, "claim_artifact_deletion", adapter_delete_wins)
    response = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert response.status_code == 204, response.text

    with session_factory() as session:
        assert session.get(Adapter, adapter["id"]) is None
        assert session.get(ManagedInputArtifact, artifact_id) is None
        job = session.scalar(
            select(ArtifactDeletionJob).where(ArtifactDeletionJob.storage_key == storage_key)
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert job is not None and job.status == "PENDING"
        assert capacity is not None and capacity.actual_bytes == 3
    assert store.stat(storage_key) is not None


def test_adapter_delete_rejects_active_upload_before_metadata_changes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-upload-delete-race")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.UPLOADING,
            size_bytes=3,
        )

    response = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "adapter_upload_in_progress"
    with session_factory() as session:
        assert session.get(Adapter, adapter["id"]) is not None


def test_adapter_delete_handoffs_charge_and_job_releases_it_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-delete-job")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(session, adapter["id"], store, size_bytes=3)

    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    with session_factory() as session:
        job = session.scalar(
            select(ArtifactDeletionJob).where(
                ArtifactDeletionJob.storage_key == artifact.storage_key
            )
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert job is not None and job.former_adapter_id == adapter["id"]
        assert job.charged_bytes == 3 and job.status == "PENDING"
        assert capacity is not None and capacity.actual_bytes == 3

        # Simulate a worker crash after the durable DELETING claim.  A later
        # cycle must reclaim only after the lease expires.
        claim = managed_input_gc.claim_deletion_job(session, job.id, now=datetime.now(UTC))
        assert claim is not None
        assert job.status == "DELETING"
        job_id = int(job.id)

    with session_factory() as session:
        stale = session.get(ArtifactDeletionJob, job_id)
        assert stale is not None
        stale.delete_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    with session_factory() as session:
        managed_input_gc.process_deletion_jobs(session, store=store)
        job = session.get(ArtifactDeletionJob, job_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert job is not None and job.status == "DELETED" and job.capacity_released_at is not None
        assert capacity is not None and capacity.actual_bytes == 0
        first_release = job.capacity_released_at
        managed_input_gc.process_deletion_jobs(session, store=store)
        session.refresh(job)
        assert job.capacity_released_at == first_release
        assert capacity.actual_bytes == 0


def test_expired_binding_is_unbound_before_gc_and_orphan_audit_is_bounded(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-binding-expiry-audit")
    store = LocalFileArtifactStore(tmp_path / "store")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.READY,
            size_bytes=3,
            expires_at=now - timedelta(seconds=1),
        )
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        config.source_type = "managed_files"
        session.add(
            AdapterInputArtifactBinding(
                adapter_id=adapter["id"],
                artifact_id=artifact.id,
                input_config_revision=config.revision,
                ordinal=0,
            )
        )

    with session_factory() as session:
        assert managed_input_gc.expire_current_bindings(session, now=now) == 1
        expired = session.get(ManagedInputArtifact, artifact.id)
        assert expired is not None and expired.status == ManagedInputArtifactStatus.PENDING_DELETE
        assert session.get(AdapterInputConfig, adapter["id"]).revision == 2

    orphan_key = store.new_storage_key()
    with store.put_part(orphan_key) as part:
        part.write(b"orphan")
    store.commit(orphan_key)
    old = (now - timedelta(minutes=10)).timestamp()
    os.utime(store.object_path(orphan_key), (old, old))
    unknown = store.root / "objects" / "unknown-directory"
    unknown.mkdir()
    (unknown / "must-stay").write_text("untouched")
    with session_factory() as session:
        result = managed_input_gc.run_orphan_audit(session, store=store, older_than=now)
    assert result.quarantined_objects == 1
    assert (unknown / "must-stay").exists()


def test_deletion_failure_audit_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    storage_key = "a" * 64
    host_path = "/private/tmp/secret-store/objects/aa/" + storage_key
    token = "EXAMPLE_TOKEN_SHOULD_NOT_BE_LOGGED"
    content = "EXAMPLE_CONTENT_SHOULD_NOT_BE_LOGGED"
    caplog.set_level(logging.INFO, logger="dlr.control.managed_input_audit")
    managed_input_gc.record_audit_event(
        "gc_delete",
        "failed",
        actor_kind="admin",
        actor_id=9,
        adapter_id=7,
        artifact_id=8,
        code="input_artifact_delete_failed",
        storage_key=storage_key,
        host_path=host_path,
        token=token,
        content=content,
    )
    assert "input_artifact_delete_failed" in caplog.text
    assert "actor_kind=admin" in caplog.text
    assert "actor_id=9" in caplog.text
    assert "adapter_id=7" in caplog.text
    assert "artifact_id=8" in caplog.text
    assert storage_key not in caplog.text
    assert host_path not in caplog.text
    assert token not in caplog.text
    assert content not in caplog.text


def test_success_audit_uses_explicit_none_code(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="dlr.control.managed_input_audit")
    managed_input_gc.record_audit_event(
        "gc_delete",
        "success",
        adapter_id=7,
        artifact_id=8,
        code=None,
    )
    assert "code=none" in caplog.text
    assert "code=unknown" not in caplog.text


@pytest.mark.parametrize(
    ("loop_name", "warning_text", "audit_code"),
    [
        ("artifact_gc_loop", "managed input GC cycle failed", "gc_cycle_failed"),
        ("orphan_audit_loop", "managed input orphan audit failed", "orphan_audit_failed"),
    ],
)
def test_background_loops_log_unexpected_failures_with_traceback(
    loop_name: str,
    warning_text: str,
    audit_code: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_to_thread(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("EXAMPLE_BACKGROUND_FAILURE")

    async def stop_after_one_cycle(_delay: float) -> None:
        raise asyncio.CancelledError

    original_to_thread = asyncio.to_thread
    original_sleep = asyncio.sleep
    monkeypatch.setattr(managed_input_gc, "_asyncio_to_thread", fail_to_thread)
    monkeypatch.setattr(managed_input_gc, "_asyncio_sleep", stop_after_one_cycle)
    caplog.set_level(logging.INFO, logger="dlr.control")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(managed_input_gc, loop_name)())
    assert asyncio.to_thread is original_to_thread
    assert asyncio.sleep is original_sleep

    warning = next(
        record
        for record in caplog.records
        if record.name == "dlr.control.managed_input_gc" and warning_text in record.getMessage()
    )
    assert warning.exc_info is not None
    assert isinstance(warning.exc_info[1], RuntimeError)
    assert any(
        record.name == "dlr.control.managed_input_audit"
        and f"code={audit_code}" in record.getMessage()
        for record in caplog.records
    )


def test_gc_delete_failure_is_backed_off_and_reclaimed(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b3-gc-delete-failure")
    store = LocalFileArtifactStore(tmp_path / "store")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.PENDING_DELETE,
            size_bytes=3,
        )

    def fail_delete(_storage_key: str) -> bool:
        raise ArtifactStoreError("simulated delete failure")

    monkeypatch.setattr(store, "delete", fail_delete)
    with session_factory() as session:
        report = managed_input_gc.process_artifact_deletions(session, store=store, now=now)
        assert report.failed == 1
        failed = session.get(ManagedInputArtifact, artifact.id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert failed is not None and failed.status == ManagedInputArtifactStatus.DELETE_FAILED
        assert failed.delete_lease_until is not None and failed.delete_lease_until > now
        assert capacity is not None and capacity.actual_bytes == 3

    monkeypatch.setattr(store, "delete", LocalFileArtifactStore.delete.__get__(store))
    with session_factory() as session:
        failed = session.get(ManagedInputArtifact, artifact.id)
        assert failed is not None
        failed.delete_lease_until = now - timedelta(seconds=1)
        session.commit()
        report = managed_input_gc.process_artifact_deletions(session, store=store, now=now)
        assert report.deleted == 1
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None and capacity.actual_bytes == 0


def test_deletion_job_failure_restarts_without_losing_charge(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b3-job-restart")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(session, adapter["id"], store, size_bytes=3)
    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204

    def fail_delete(_storage_key: str) -> bool:
        raise ArtifactStoreError("simulated job delete failure")

    monkeypatch.setattr(store, "delete", fail_delete)
    with session_factory() as session:
        first = managed_input_gc.process_deletion_jobs(session, store=store)
        assert first.failed == 1
        job = session.scalar(
            select(ArtifactDeletionJob).where(
                ArtifactDeletionJob.storage_key == artifact.storage_key
            )
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert job is not None and job.status == "DELETE_FAILED"
        assert capacity is not None and capacity.actual_bytes == 3
        job.delete_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    monkeypatch.setattr(store, "delete", LocalFileArtifactStore.delete.__get__(store))
    with session_factory() as session:
        second = managed_input_gc.process_deletion_jobs(session, store=store)
        assert second.completed == 1
        job = session.scalar(
            select(ArtifactDeletionJob).where(
                ArtifactDeletionJob.storage_key == artifact.storage_key
            )
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert job is not None and job.status == "DELETED"
        assert job.capacity_released_at is not None
        assert capacity is not None and capacity.actual_bytes == 0


def test_delete_failure_threshold_stops_automatic_retry_until_admin_releases_it(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-threshold-admin-retry")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.DELETE_FAILED,
            size_bytes=3,
        )
        artifact.delete_attempts = settings.artifact_delete_alert_threshold
        artifact.delete_lease_until = None
        artifact_id = int(artifact.id)

    with session_factory() as session:
        stopped = managed_input_gc.process_artifact_deletions(session, store=store)
        assert stopped.claimed == 0
        assert (
            managed_input_gc.claim_artifact_deletion(
                session,
                artifact_id,
                force=False,
                protection_hook=lambda _session, _artifact_id: False,
            )
            is None
        )
    released = api_client.post(f"/api/system/managed-input-artifacts/{artifact_id}/retry-delete")
    assert released.status_code == 204, released.text
    with session_factory() as session:
        current = session.get(ManagedInputArtifact, artifact_id)
        assert current is not None
        assert current.status == ManagedInputArtifactStatus.PENDING_DELETE
        assert current.delete_attempts == settings.artifact_delete_alert_threshold
        retried = managed_input_gc.process_artifact_deletions(session, store=store)
        assert retried.deleted == 1


def test_thresholded_artifact_rejects_business_delete_until_admin_retry(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-threshold-business-delete")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.DELETE_FAILED,
            size_bytes=3,
        )
        artifact.delete_attempts = settings.artifact_delete_alert_threshold
        artifact.delete_lease_until = None
        artifact_id = int(artifact.id)
        storage_key = artifact.storage_key

    response = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")

    assert response.status_code == 409, response.text
    assert (
        response.json()["detail"]["code"] == ManagedInputErrorCode.ARTIFACT_RETRY_NOT_ALLOWED.value
    )
    with session_factory() as session:
        current = session.get(ManagedInputArtifact, artifact_id)
        capacity = session.get(ManagedInputCapacity, 1)
        assert current is not None
        assert current.status == ManagedInputArtifactStatus.DELETE_FAILED
        assert current.delete_attempts == settings.artifact_delete_alert_threshold
        assert capacity is not None and capacity.actual_bytes == 3
        assert store.stat(storage_key) is not None


def test_admin_retry_rejects_subthreshold_artifact_backoff(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-subthreshold-artifact-retry")
    store = LocalFileArtifactStore(tmp_path / "store")
    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    with session_factory.begin() as session:
        artifact = create_artifact(
            session,
            adapter["id"],
            store,
            status=ManagedInputArtifactStatus.DELETE_FAILED,
            size_bytes=3,
        )
        artifact.delete_attempts = settings.artifact_delete_alert_threshold - 1
        artifact.delete_lease_until = retry_at
        artifact_id = int(artifact.id)

    response = api_client.post(f"/api/system/managed-input-artifacts/{artifact_id}/retry-delete")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "input_artifact_retry_not_allowed"
    with session_factory() as session:
        current = session.get(ManagedInputArtifact, artifact_id)
        assert current is not None
        assert current.status == ManagedInputArtifactStatus.DELETE_FAILED
        assert current.delete_attempts == settings.artifact_delete_alert_threshold - 1
        assert current.delete_lease_until == retry_at


def test_deletion_job_threshold_requires_admin_retry_and_uses_contract_fields(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-job-threshold-admin-retry")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(session, adapter["id"], store, size_bytes=3)
        storage_key = artifact.storage_key
    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    with session_factory.begin() as session:
        job = session.scalar(
            select(ArtifactDeletionJob).where(ArtifactDeletionJob.storage_key == storage_key)
        )
        assert job is not None
        job.status = ManagedInputDeletionJobStatus.DELETE_FAILED
        job.delete_attempts = settings.artifact_delete_alert_threshold
        job.delete_started_at = datetime.now(UTC)
        job.delete_lease_until = None
        job_id = int(job.id)

    with session_factory() as session:
        stopped = managed_input_gc.process_deletion_jobs(session, store=store)
        assert stopped.claimed == 0
    released = api_client.post(f"/api/system/managed-input-deletion-jobs/{job_id}/retry-delete")
    assert released.status_code == 204, released.text
    with session_factory() as session:
        job = session.get(ArtifactDeletionJob, job_id)
        assert job is not None
        assert job.status == ManagedInputDeletionJobStatus.PENDING
        assert job.delete_attempts == settings.artifact_delete_alert_threshold
        assert job.delete_started_at is None
        retried = managed_input_gc.process_deletion_jobs(session, store=store)
        assert retried.completed == 1
        session.refresh(job)
        assert job.status == ManagedInputDeletionJobStatus.DELETED


def test_admin_retry_rejects_subthreshold_deletion_job_backoff(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    adapter = create_task(api_client, "b3-subthreshold-job-retry")
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory.begin() as session:
        artifact = create_artifact(session, adapter["id"], store, size_bytes=3)
        storage_key = artifact.storage_key
    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    with session_factory.begin() as session:
        job = session.scalar(
            select(ArtifactDeletionJob).where(ArtifactDeletionJob.storage_key == storage_key)
        )
        assert job is not None
        job.status = ManagedInputDeletionJobStatus.DELETE_FAILED
        job.delete_attempts = settings.artifact_delete_alert_threshold - 1
        job.delete_lease_until = retry_at
        job_id = int(job.id)

    response = api_client.post(f"/api/system/managed-input-deletion-jobs/{job_id}/retry-delete")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "input_deletion_job_retry_not_allowed"
    with session_factory() as session:
        job = session.get(ArtifactDeletionJob, job_id)
        assert job is not None
        assert job.status == ManagedInputDeletionJobStatus.DELETE_FAILED
        assert job.delete_attempts == settings.artifact_delete_alert_threshold - 1
        assert job.delete_lease_until == retry_at
