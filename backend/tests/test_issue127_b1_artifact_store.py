"""Issue #127 B1 storage, upload, and reservation contract tests."""

from __future__ import annotations

import asyncio
import errno
import io
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from dlr.common.config import settings
from dlr.control.api import managed_input as managed_input_api
from dlr.control.models import (
    Adapter,
    AdapterPermission,
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputReservationStatus,
    ManagedInputSettings,
    ManagedInputUploadReservation,
    User,
)
from dlr.control.security import (
    ACCOUNT_ENTRY_MODE,
    ENTRY_MODE_SCOPE_KEY,
    Principal,
    require_principal,
    require_upload_principal,
)
from dlr.control.services import artifact_store as artifact_store_module
from dlr.control.services import managed_input_upload
from dlr.control.services.accounts import SESSION_COOKIE_NAME, create_session
from dlr.control.services.artifact_store import (
    ArtifactStoreAtomicityError,
    ArtifactStoreSecurityError,
    LocalFileArtifactStore,
)
from dlr.control.services.managed_input_upload import UploadSessionState


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


def test_streaming_upload_offloads_blocking_work_and_batches_reservations(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-streaming-workers")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    set_policy(session_factory, max_file_bytes=4 * 1024 * 1024)

    boundary = "b1-streaming-workers"
    payload = b"x" * (3 * 1024 * 1024 + 123)
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="stream.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    suffix = f"\r\n--{boundary}--\r\n".encode()
    chunk_size = 64 * 1024
    frames = [prefix]
    frames.extend(
        payload[offset : offset + chunk_size] for offset in range(0, len(payload), chunk_size)
    )
    frames.append(suffix)
    frame_iterator = iter(frames)
    remaining = len(frames)

    async def receive_body() -> dict[str, object]:
        nonlocal remaining
        try:
            body = next(frame_iterator)
        except StopIteration:
            return {"type": "http.disconnect"}
        remaining -= 1
        return {"type": "http.request", "body": body, "more_body": remaining > 0}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/adapters/{adapter['id']}/input-artifacts",
            "raw_path": f"/api/adapters/{adapter['id']}/input-artifacts".encode(),
            "query_string": b"",
            "headers": [
                (
                    b"content-type",
                    f"multipart/form-data; boundary={boundary}".encode(),
                )
            ],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "root_path": "",
        },
        receive_body,
    )

    original_check = managed_input_upload.check_stream_low_watermark_bytes
    original_expand = managed_input_upload.expand_upload_reservation
    original_run_in_session = managed_input_api._run_in_session
    expansion_calls = 0
    session_calls = 0
    active_sessions = 0
    max_active_sessions = 0
    heartbeat_count = 0
    lock = threading.Lock()

    def slow_check(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.01)
        original_check(*args, **kwargs)

    def counted_expand(*args: Any, **kwargs: Any) -> Any:
        nonlocal expansion_calls
        with lock:
            expansion_calls += 1
        return original_expand(*args, **kwargs)

    def tracked_session(operation: Any) -> Any:
        nonlocal session_calls, active_sessions, max_active_sessions
        with lock:
            session_calls += 1
            active_sessions += 1
            max_active_sessions = max(max_active_sessions, active_sessions)
        try:
            return original_run_in_session(operation)
        finally:
            with lock:
                active_sessions -= 1

    monkeypatch.setattr(managed_input_upload, "check_stream_low_watermark_bytes", slow_check)
    monkeypatch.setattr(managed_input_upload, "expand_upload_reservation", counted_expand)
    monkeypatch.setattr(managed_input_api, "_run_in_session", tracked_session)

    async def run_upload() -> Any:
        finished = asyncio.Event()

        async def upload() -> Any:
            try:
                return await managed_input_api._stream_upload(
                    request, adapter["id"], Principal(kind="superadmin")
                )
            finally:
                finished.set()

        async def heartbeat() -> None:
            nonlocal heartbeat_count
            while not finished.is_set():
                heartbeat_count += 1
                await asyncio.sleep(0)

        result, _ = await asyncio.gather(upload(), heartbeat())
        return result

    result = asyncio.run(run_upload())
    assert result.size_bytes == len(payload)
    assert expansion_calls <= 4
    assert session_calls >= 5
    assert active_sessions == 0
    assert max_active_sessions == 1
    assert heartbeat_count >= 20


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
    assert guessed.status_code == 204, guessed.text
    assert "storage_key" not in guessed.text
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None and artifact.status == "STAGED"

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


def test_regular_and_stream_upload_principals_share_session_validation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    def account_request(raw_token: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/adapters",
                "headers": [(b"cookie", f"{SESSION_COOKIE_NAME}={raw_token}".encode())],
                ENTRY_MODE_SCOPE_KEY: ACCOUNT_ENTRY_MODE,
            }
        )

    with session_factory() as session:
        user = User(
            username="b1-shared-session-helper",
            password_hash="anonymous-test-hash",
            role="admin",
            must_change_password=False,
        )
        session.add(user)
        session.flush()
        raw_token = create_session(session, user)
        request = account_request(raw_token)

        regular_principal = require_principal(request, session)
        upload_principal = require_upload_principal(request)
        assert upload_principal == regular_principal

        user.must_change_password = True
        session.commit()
        with pytest.raises(HTTPException) as regular_error:
            require_principal(request, session)
        with pytest.raises(HTTPException) as upload_error:
            require_upload_principal(request)

    assert regular_error.value.status_code == upload_error.value.status_code == 403
    assert (
        regular_error.value.detail
        == upload_error.value.detail
        == {
            "code": "account_password_change_required",
            "message": "Change the account password before using the application",
        }
    )


def test_stream_low_watermark_snapshot_refreshes_at_reservation_growth(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-watermark-boundary")
    store = LocalFileArtifactStore(tmp_path / "store")
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    initial_watermark = 64 * 1024 * 1024
    refreshed_watermark = 128 * 1024 * 1024
    set_policy(session_factory, min_free_space_bytes=initial_watermark)

    with session_factory() as session:
        state = managed_input_upload.begin_upload(
            session,
            adapter["id"],
            original_filename="watermark.txt",
            content_type="text/plain",
            store=store,
        )
    assert state.min_free_space_bytes == initial_watermark

    set_policy(session_factory, min_free_space_bytes=refreshed_watermark)
    monkeypatch.setattr(managed_input_upload, "_free_bytes", lambda _store: initial_watermark + 2)
    managed_input_upload.check_stream_low_watermark_bytes(store, state.min_free_space_bytes, 1)

    monkeypatch.setattr(
        managed_input_upload,
        "_free_bytes",
        lambda _store: refreshed_watermark + managed_input_upload.RESERVATION_GROWTH_BYTES + 2,
    )
    with session_factory() as session:
        expanded = managed_input_upload.expand_upload_reservation(
            session,
            adapter["id"],
            state.upload_session_id,
            state.reserved_bytes + 1,
            store=store,
            growth_bytes=managed_input_upload.RESERVATION_GROWTH_BYTES,
        )
    assert expanded.min_free_space_bytes == refreshed_watermark

    monkeypatch.setattr(managed_input_upload, "_free_bytes", lambda _store: initial_watermark + 2)
    with pytest.raises(HTTPException) as watermark_error:
        managed_input_upload.check_stream_low_watermark_bytes(
            store, expanded.min_free_space_bytes, 1
        )
    assert watermark_error.value.status_code == 507

    with session_factory() as session:
        managed_input_upload.abort_upload(
            session, adapter["id"], expanded.upload_session_id, store=store
        )


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


def test_expiry_reclaims_reservation_for_archived_adapter(
    session_factory: sessionmaker[Session],
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-archived-expiry")
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory() as session:
        state = managed_input_upload.begin_upload(
            session,
            adapter["id"],
            original_filename="archived.txt",
            content_type="text/plain",
            store=store,
        )
        row = session.get(ManagedInputUploadReservation, state.reservation_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        archived = session.get(Adapter, adapter["id"])
        assert archived is not None
        archived.archived_at = datetime.now(UTC)
        session.commit()

    with session_factory() as session:
        assert (
            managed_input_upload.expire_upload_reservations(
                session, store=store, now=datetime.now(UTC), limit=10
            )
            == 1
        )

    with session_factory() as session:
        reservation = session.get(ManagedInputUploadReservation, state.reservation_id)
        assert reservation is not None
        assert reservation.status == ManagedInputReservationStatus.EXPIRED
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.reserved_bytes == 0


def test_upload_session_progress_has_no_missing_part_side_effect(
    tmp_path: Path,
) -> None:
    store = LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    prefix = store.parts_root / key[:2]
    state = UploadSessionState(
        adapter_id=1,
        artifact_id=1,
        reservation_id=1,
        upload_session_id="session",
        storage_key=key,
        reserved_bytes=64 * 1024,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    response = managed_input_upload.upload_session_response(state, store)
    assert response.received_bytes == 0
    assert not prefix.exists()


def test_upload_session_progress_tolerates_concurrent_part_delete(tmp_path: Path) -> None:
    store = LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    with store.put_part(key) as part:
        part.write(b"partial")
    state = UploadSessionState(
        adapter_id=1,
        artifact_id=1,
        reservation_id=1,
        upload_session_id="session",
        storage_key=key,
        reserved_bytes=64 * 1024,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    observed = threading.Event()
    deleted = threading.Event()
    original_stat_part = store.stat_part

    def stat_then_delete(storage_key: str) -> Any:
        result = original_stat_part(storage_key)
        observed.set()
        assert deleted.wait(timeout=5)
        return result

    def delete_part() -> None:
        assert observed.wait(timeout=5)
        store.delete_part(key)
        deleted.set()

    deleter = threading.Thread(target=delete_part)
    deleter.start()
    store.stat_part = stat_then_delete  # type: ignore[method-assign]
    response = managed_input_upload.upload_session_response(state, store)
    deleter.join(timeout=5)
    assert not deleter.is_alive()
    assert response.received_bytes == 7
    assert not store.stat_part(key)


def test_recover_and_renew_map_store_failures_to_stable_code(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_task(api_client, "b1-session-store-failure")
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

    def unavailable_store(_root: str) -> LocalFileArtifactStore:
        raise OSError("simulated store outage")

    monkeypatch.setattr(managed_input_api, "LocalFileArtifactStore", unavailable_store)
    recovered = api_client.get(
        f"/api/adapters/{adapter['id']}/input-artifacts/sessions/{state.upload_session_id}"
    )
    renewed = api_client.post(
        f"/api/adapters/{adapter['id']}/input-artifacts/sessions/{state.upload_session_id}/renew"
    )
    assert recovered.status_code == 503
    assert recovered.json()["detail"]["code"] == "artifact_store_unavailable"
    assert renewed.status_code == 503
    assert renewed.json()["detail"]["code"] == "artifact_store_unavailable"


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
