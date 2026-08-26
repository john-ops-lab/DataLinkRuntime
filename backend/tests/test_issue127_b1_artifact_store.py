"""Issue #127 B1 storage, upload, and reservation contract tests."""

from __future__ import annotations

import errno
import io
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    AdapterPermission,
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputReservationStatus,
    ManagedInputSettings,
    ManagedInputUploadReservation,
    User,
)
from dlr.control.security import Principal, require_principal
from dlr.control.services import artifact_store as artifact_store_module
from dlr.control.services import managed_input_upload
from dlr.control.services.artifact_store import (
    ArtifactStoreAtomicityError,
    ArtifactStoreSecurityError,
    LocalFileArtifactStore,
)


def create_task(api_client: TestClient, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def set_policy(
    session_factory: sessionmaker[Session],
    *,
    max_file_bytes: int | None = None,
    adapter_quota_bytes: int | None = None,
    platform_quota_bytes: int | None = None,
    min_free_space_bytes: int | None = None,
) -> None:
    with session_factory() as session:
        policy = session.get(ManagedInputSettings, 1)
        assert policy is not None
        if max_file_bytes is not None:
            policy.max_file_bytes = max_file_bytes
        if adapter_quota_bytes is not None:
            policy.adapter_quota_bytes = adapter_quota_bytes
        if platform_quota_bytes is not None:
            policy.platform_quota_bytes = platform_quota_bytes
        if min_free_space_bytes is not None:
            policy.min_free_space_bytes = min_free_space_bytes
        session.commit()


def test_store_publishes_atomically_and_rejects_unsafe_paths(tmp_path: Path) -> None:
    store = LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()

    with store.put_part(key) as part:
        part.write(b"hello")
    store.commit(key)

    published = store.stat(key)
    assert published is not None
    assert published.size_bytes == 5
    with store.open(key) as handle:
        assert handle.read() == b"hello"
    assert not store.part_path(key).exists()
    assert store.delete(key) is True
    assert store.delete(key) is True
    assert store.stat(key) is None

    for unsafe_key in ("../outside", "a" * 63, "A" * 64, ""):
        with pytest.raises(ArtifactStoreSecurityError):
            store.stat(unsafe_key)


def test_store_rejects_symlink_prefix_and_cross_filesystem_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    store = LocalFileArtifactStore(store_root)
    key = store.new_storage_key()
    outside = tmp_path / "outside"
    outside.mkdir()
    prefix = store.part_path(key).parent
    prefix.rmdir()
    prefix.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactStoreSecurityError), store.put_part(key):
        pass
    assert not any(outside.iterdir())

    original_stat = artifact_store_module.os.stat

    def fake_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_stat(path, *args, **kwargs)
        path_text = os.fspath(path)
        if path_text.endswith("/objects"):
            return SimpleNamespace(st_dev=101)
        if path_text.endswith("/parts"):
            return SimpleNamespace(st_dev=202)
        if path_text.endswith("/quarantine"):
            return SimpleNamespace(st_dev=101)
        return result

    monkeypatch.setattr(artifact_store_module.os, "stat", fake_stat)
    with pytest.raises(ArtifactStoreAtomicityError):
        LocalFileArtifactStore(tmp_path / "cross-device")


def test_store_converts_exdev_to_atomicity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    with store.put_part(key) as part:
        part.write(b"payload")

    def fail_rename(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(artifact_store_module.os, "rename", fail_rename)
    with pytest.raises(ArtifactStoreAtomicityError):
        store.commit(key)
    assert store.part_path(key).exists()


def test_upload_uses_actual_stream_size_not_content_length_and_redacts_metadata(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = create_task(api_client, "b1-stream-size")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    set_policy(session_factory, max_file_bytes=1024 * 1024)

    body = b"actual bytes and not the forged length"
    secret_body = b"EXAMPLE_UPLOAD_CONTENT_SHOULD_NOT_BE_LOGGED"
    caplog.set_level(logging.INFO, logger="dlr.control")
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("report.txt", io.BytesIO(body + secret_body), "image/png")},
        headers={"Content-Length": "1"},
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["original_filename"] == "report.txt"
    assert payload["content_type"] == "image/png"
    assert payload["size_bytes"] == len(body + secret_body)
    assert len(payload["sha256"]) == 64
    assert "storage_key" not in response.text
    assert str(tmp_path) not in response.text
    assert ".part" not in response.text
    assert secret_body.decode() not in caplog.text


def test_upload_path_filename_is_metadata_only_and_extension_is_authoritative(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-filename-traversal")
    store_root = tmp_path / "store"
    outside = tmp_path / "outside.txt"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": (f"../{outside.name}", b"never escape", "application/zip")},
    )
    assert response.status_code == 201, response.text
    assert not outside.exists()
    assert response.json()["content_type"] == "application/zip"

    blocked = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("payload.zip", b"not a zip", "text/plain")},
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "input_file_type_not_allowed"

    with session_factory() as session:
        assert (
            session.scalar(
                select(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == adapter["id"])
            )
            is not None
        )


def test_upload_rejects_actual_oversize_and_cleans_reservation_and_part(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-actual-limit")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    set_policy(session_factory, max_file_bytes=1024 * 1024)

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("too-large.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
        headers={"Content-Length": "1"},
    )
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "input_file_too_large"

    with session_factory() as session:
        assert (
            session.scalar(
                select(ManagedInputArtifact).where(
                    ManagedInputArtifact.adapter_id == adapter["id"],
                    ManagedInputArtifact.status == "UPLOADING",
                )
            )
            is None
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == 0
        assert capacity.reserved_bytes == 0
    assert not list((store_root / "parts").rglob("*.part"))
    assert not list((store_root / "objects").rglob("*"))


def test_interrupted_multipart_upload_is_terminal_and_compensated(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-interrupted-multipart")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)

    boundary = "b1-interruption"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="partial.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "partial payload without a closing boundary"
    ).encode()
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "input_upload_interrupted"
    with session_factory() as session:
        artifact = session.scalar(
            select(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == adapter["id"])
        )
        assert artifact is not None
        assert artifact.status == "DELETED"
        assert artifact.last_error_code == "input_upload_interrupted"
        reservation = session.get(ManagedInputUploadReservation, artifact.upload_reservation_id)
        assert reservation is not None
        assert reservation.status == ManagedInputReservationStatus.CANCELLED
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.reserved_bytes == 0
    assert not list((store_root / "parts").rglob("*.part"))
    assert not list((store_root / "objects").rglob("*"))


def test_staged_list_delete_is_adapter_scoped_and_keeps_revision(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    first = create_task(api_client, "b1-staged-owner")
    second = create_task(api_client, "b1-staged-other")

    uploaded = api_client.post(
        f"/api/adapters/{first['id']}/input-artifacts",
        files={"file": ("one.txt", b"one", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact_id = uploaded.json()["id"]

    visible = api_client.get(f"/api/adapters/{first['id']}/input-artifacts?status=staged")
    assert visible.status_code == 200, visible.text
    assert [item["id"] for item in visible.json()] == [artifact_id]
    assert "storage_key" not in visible.text

    hidden = api_client.get(f"/api/adapters/{second['id']}/input-artifacts?status=staged")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == []

    guessed = api_client.delete(f"/api/adapters/{second['id']}/input-artifacts/{artifact_id}")
    assert guessed.status_code == 404, guessed.text
    assert "storage_key" not in guessed.text

    before = api_client.get(f"/api/adapters/{first['id']}/input-config").json()["revision"]
    deleted = api_client.delete(f"/api/adapters/{first['id']}/input-artifacts/{artifact_id}")
    assert deleted.status_code == 204, deleted.text
    repeated = api_client.delete(f"/api/adapters/{first['id']}/input-artifacts/{artifact_id}")
    assert repeated.status_code == 204, repeated.text
    after = api_client.get(f"/api/adapters/{first['id']}/input-config").json()["revision"]
    assert after == before

    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        assert artifact.status == "DELETED"
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == 0


def test_delete_failure_keeps_charge_and_retry_is_idempotent(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-delete-retry")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    uploaded = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("retry.txt", b"retry", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact_id = uploaded.json()["id"]

    original_delete = LocalFileArtifactStore.delete

    def fail_delete(self: LocalFileArtifactStore, storage_key: str) -> bool:
        raise OSError("simulated delete failure")

    monkeypatch.setattr(LocalFileArtifactStore, "delete", fail_delete)
    failed = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert failed.status_code == 503, failed.text
    assert failed.json()["detail"]["code"] == "input_artifact_delete_failed"
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        assert artifact.status == "DELETE_FAILED"
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == 5

    monkeypatch.setattr(LocalFileArtifactStore, "delete", original_delete)
    retried = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert retried.status_code == 204, retried.text
    repeated = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert repeated.status_code == 204, repeated.text
    with session_factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == 0


def test_feature_flag_rejects_upload_before_store_creation(
    api_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_task(api_client, "b1-feature-flag")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", False)

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("disabled.txt", b"no", "text/plain")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "input_source_not_available"
    assert not (tmp_path / "store").exists()


def test_low_watermark_rejects_before_any_upload_bytes_are_written(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-low-watermark")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    minimum = 64 * 1024 * 1024
    set_policy(session_factory, min_free_space_bytes=minimum)
    monkeypatch.setattr(managed_input_upload, "_free_bytes", lambda store: minimum)

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("low.txt", b"blocked", "text/plain")},
    )
    assert response.status_code == 507, response.text
    assert response.json()["detail"]["code"] == "artifact_store_low_watermark"
    with session_factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.reserved_bytes == 0
    assert not list((store_root / "objects").rglob("*"))


def test_staged_artifact_routes_require_adapter_edit_acl(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-acl")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    uploaded = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts",
        files={"file": ("acl.txt", b"acl", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact_id = uploaded.json()["id"]
    with session_factory() as session:
        reader = User(
            username="b1-acl-reader",
            password_hash="anonymous-test-hash",
            role="user",
        )
        session.add(reader)
        session.flush()
        session.add(
            AdapterPermission(adapter_id=adapter["id"], user_id=reader.id, permission="read")
        )
        session.commit()
        reader_id = reader.id

    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=reader_id, username="b1-acl-reader"
    )
    try:
        listed = api_client.get(f"/api/adapters/{adapter['id']}/input-artifacts?status=staged")
        deleted = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)
    assert listed.status_code == 403, listed.text
    assert listed.json()["detail"]["code"] == "adapter_read_only"
    assert deleted.status_code == 403, deleted.text
    assert deleted.json()["detail"]["code"] == "adapter_read_only"


def test_active_upload_session_recovers_progress_and_renews_safely(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-session-recovery")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory() as session:
        state = managed_input_upload.begin_upload(
            session,
            adapter["id"],
            original_filename="recover.txt",
            content_type="text/plain",
            store=store,
        )
    with store.put_part(state.storage_key) as part:
        part.write(b"partial")

    recovered = api_client.get(
        f"/api/adapters/{adapter['id']}/input-artifacts/sessions/{state.upload_session_id}"
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["artifact_id"] == state.artifact_id
    assert recovered.json()["status"] == "UPLOADING"
    assert recovered.json()["received_bytes"] == 7
    assert "storage_key" not in recovered.text
    renewed = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts/sessions/{state.upload_session_id}/renew"
    )
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["received_bytes"] == 7

    with session_factory() as session:
        managed_input_upload.abort_upload(
            session, adapter["id"], state.upload_session_id, store=store
        )


def test_reservation_expansion_allows_only_one_writer_at_last_quota(
    session_factory: sessionmaker[Session],
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-reservation-race")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    set_policy(
        session_factory, adapter_quota_bytes=1024 * 1024, platform_quota_bytes=2 * 1024 * 1024
    )
    store = LocalFileArtifactStore(tmp_path / "store")

    states = []
    for _ in range(2):
        with session_factory() as session:
            states.append(
                managed_input_upload.begin_upload(
                    session,
                    adapter["id"],
                    original_filename="race.txt",
                    content_type="text/plain",
                    store=store,
                )
            )

    barrier = threading.Barrier(2)
    results: list[BaseException | None] = [None, None]

    def expand(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            with session_factory() as session:
                managed_input_upload.expand_upload_reservation(
                    session,
                    adapter["id"],
                    states[index].upload_session_id,
                    600 * 1024,
                    store=store,
                )
        except BaseException as exc:  # captured to keep the race test deterministic
            results[index] = exc

    threads = [threading.Thread(target=expand, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert sum(isinstance(result, BaseException) for result in results) == 1

    with session_factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.reserved_bytes == 600 * 1024 + 64 * 1024
    for state in states:
        with session_factory() as session:
            managed_input_upload.abort_upload(
                session, adapter["id"], state.upload_session_id, error_code="test_cleanup"
            )


def test_expired_reservation_is_terminal_and_reclaimed_once(
    session_factory: sessionmaker[Session],
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-expiry-race")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory() as session:
        state = managed_input_upload.begin_upload(
            session,
            adapter["id"],
            original_filename="expired.txt",
            content_type="text/plain",
            store=store,
        )
        reservation = session.get(ManagedInputUploadReservation, state.reservation_id)
        assert reservation is not None
        reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    outcomes: list[int] = []
    barrier = threading.Barrier(2)

    def expire() -> None:
        barrier.wait(timeout=5)
        with session_factory() as session:
            outcomes.append(
                managed_input_upload.expire_upload_reservations(
                    session, store=store, now=datetime.now(UTC), limit=10
                )
            )

    threads = [threading.Thread(target=expire) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(outcomes) == [0, 1]

    with session_factory() as session:
        reservation = session.get(ManagedInputUploadReservation, state.reservation_id)
        assert reservation is not None
        assert reservation.status == ManagedInputReservationStatus.EXPIRED
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.reserved_bytes == 0


def test_orphan_audit_quarantines_only_old_random_objects(tmp_path: Path) -> None:
    store = LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    with store.put_part(key) as part:
        part.write(b"orphan")
    store.commit(key)
    object_path = store.object_path(key)
    old = datetime.now(UTC) - timedelta(hours=2)
    os.utime(object_path, (old.timestamp(), old.timestamp()))

    result = store.audit_orphans(set(), older_than=datetime.now(UTC) - timedelta(minutes=1))
    assert result.inspected_objects == 1
    assert result.quarantined_objects == 1
    assert not object_path.exists()
    assert store.quarantine_path(key).exists()
