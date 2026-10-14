"""Real PostgreSQL regressions for quota-bounded upload batching (Issue #198)."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputUploadReservation,
)
from dlr.control.services import managed_input_upload
from dlr.control.services.artifact_store import LocalFileArtifactStore
from test_issue127_b1_artifact_store import create_task, set_policy

MIB = 1024 * 1024


def _upload(client: TestClient, adapter_id: int, payload: bytes):
    return client.post(
        f"/api/adapters/{adapter_id}/input-artifacts",
        files={"file": ("limit.txt", payload, "text/plain")},
    )


def _assert_ledger(factory: sessionmaker[Session], actual: int, reserved: int = 0) -> None:
    with factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert (capacity.actual_bytes, capacity.reserved_bytes) == (actual, reserved)
        active = session.scalar(
            select(func.coalesce(func.sum(ManagedInputUploadReservation.reserved_bytes), 0)).where(
                ManagedInputUploadReservation.status == "ACTIVE"
            )
        )
        assert active == reserved


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalFileArtifactStore:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    return LocalFileArtifactStore(tmp_path / "store")


@pytest.mark.parametrize("file_limit", [MIB, 100 * MIB])
def test_exact_mib_upload_is_readable_and_saveable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_limit: int,
) -> None:
    store = _configure(monkeypatch, tmp_path)
    adapter_id = create_task(api_client, "issue198-exact-mib")["id"]
    set_policy(session_factory, max_file_bytes=file_limit, adapter_quota_bytes=MIB)
    payload = b"x" * MIB
    response = _upload(api_client, adapter_id, payload)
    assert response.status_code == 201, response.text
    artifact_id = response.json()["id"]
    assert response.json()["size_bytes"] == MIB
    _assert_ledger(session_factory, MIB)
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        with store.open(artifact.storage_key) as handle:
            stored = handle.read()
        assert stored == payload
        assert hashlib.sha256(stored).hexdigest() == artifact.sha256
        assert artifact.sha256 == "8f990ba0b577b51cf009ea049368c16bbda1b21e1b93be07a824758bb253c39b"
    saved = api_client.put(
        f"/api/adapters/{adapter_id}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default"},
        },
    )
    assert saved.status_code == 200, saved.text
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None and artifact.status == "READY"
    rejected = _upload(api_client, adapter_id, b"x")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "adapter_input_quota_exceeded"
    _assert_ledger(session_factory, MIB)


@pytest.mark.parametrize("scope", ["adapter", "platform"])
@pytest.mark.parametrize("file_limit", [MIB, 2 * MIB, 100 * MIB])
@pytest.mark.parametrize("extra_byte", [0, 1])
def test_remaining_quota_boundary_and_file_limit(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    file_limit: int,
    extra_byte: int,
) -> None:
    store = _configure(monkeypatch, tmp_path)
    adapter_id = create_task(api_client, "issue198-boundary")["id"]
    seed = b"already charged!!"
    seed_adapter_id = (
        adapter_id
        if scope == "adapter"
        else create_task(api_client, "issue198-platform-seed")["id"]
    )
    seeded = _upload(api_client, seed_adapter_id, seed)
    assert seeded.status_code == 201, seeded.text
    set_policy(
        session_factory,
        max_file_bytes=file_limit,
        adapter_quota_bytes=2 * MIB + len(seed),
        platform_quota_bytes=2 * MIB + len(seed) if scope == "platform" else 4 * MIB,
    )
    payload = b"x" * (min(file_limit, 2 * MIB) + extra_byte)
    response = _upload(api_client, adapter_id, payload)
    if extra_byte:
        expected = (
            "input_file_too_large" if file_limit <= 2 * MIB else f"{scope}_input_quota_exceeded"
        )
        assert response.status_code == (413 if file_limit <= 2 * MIB else 409), response.text
        assert response.json()["detail"]["code"] == expected
        _assert_ledger(session_factory, len(seed))
        assert not list((store.root / "parts").rglob("*.part"))
        with session_factory() as session:
            failed = session.scalars(
                select(ManagedInputArtifact).where(ManagedInputArtifact.status == "DELETED")
            ).all()
            assert failed
            assert all(store.stat(row.storage_key) is None for row in failed)
    else:
        assert response.status_code == 201, response.text
        _assert_ledger(session_factory, len(seed) + len(payload))
        delete_path = f"/api/adapters/{adapter_id}/input-artifacts/{response.json()['id']}"
        deleted = api_client.delete(delete_path)
        assert deleted.status_code == 204, deleted.text
        _assert_ledger(session_factory, len(seed))
        assert api_client.delete(delete_path).status_code == 204
        _assert_ledger(session_factory, len(seed))
        assert _upload(api_client, adapter_id, payload).status_code == 201


@pytest.mark.parametrize("scope", ["adapter", "platform"])
@pytest.mark.parametrize("remaining", [1, 32768, 65535])
def test_small_remaining_quota_bounds_initial_reservation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    remaining: int,
) -> None:
    _configure(monkeypatch, tmp_path)
    adapter_id = create_task(api_client, "issue198-small-remainder")["id"]
    seed_adapter_id = (
        adapter_id if scope == "adapter" else create_task(api_client, "issue198-small-seed")["id"]
    )
    seed_size = MIB - remaining
    seeded = _upload(api_client, seed_adapter_id, b"s" * seed_size)
    assert seeded.status_code == 201, seeded.text
    set_policy(
        session_factory,
        adapter_quota_bytes=MIB,
        platform_quota_bytes=MIB if scope == "platform" else 2 * MIB,
    )
    response = _upload(api_client, adapter_id, b"x" * remaining)
    assert response.status_code == 201, response.text
    _assert_ledger(session_factory, MIB)
    rejected = _upload(api_client, adapter_id, b"x")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == f"{scope}_input_quota_exceeded"
    _assert_ledger(session_factory, MIB)


@pytest.mark.parametrize("scope", ["adapter", "platform"])
def test_batched_growth_cannot_oversell_concurrent_quota(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    store = _configure(monkeypatch, tmp_path)
    first = create_task(api_client, "issue198-race-first")["id"]
    second = first if scope == "adapter" else create_task(api_client, "issue198-race-second")["id"]
    set_policy(
        session_factory,
        adapter_quota_bytes=MIB,
        platform_quota_bytes=MIB if scope == "platform" else 4 * MIB,
    )
    states = []
    for adapter_id in (first, second):
        with session_factory() as session:
            states.append(
                managed_input_upload.begin_upload(
                    session,
                    adapter_id,
                    original_filename="race.txt",
                    content_type="text/plain",
                    store=store,
                )
            )
    barrier = threading.Barrier(2)
    outcomes: list[object] = [None, None]

    def expand(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            state = states[index]
            with session_factory() as session:
                outcomes[index] = managed_input_upload.expand_upload_reservation(
                    session,
                    state.adapter_id,
                    state.upload_session_id,
                    600 * 1024,
                    store=store,
                    growth_bytes=managed_input_upload.RESERVATION_GROWTH_BYTES,
                )
        except Exception as error:
            outcomes[index] = error

    threads = [threading.Thread(target=expand, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    failures = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
    assert len(failures) == 1, outcomes
    assert failures[0].detail["code"] == f"{scope}_input_quota_exceeded"
    _assert_ledger(session_factory, 0, MIB)
    for state in states:
        with session_factory() as session:
            managed_input_upload.abort_upload(
                session, state.adapter_id, state.upload_session_id, store=store
            )
    _assert_ledger(session_factory, 0)
    assert _upload(api_client, first, b"x" * MIB).status_code == 201
    _assert_ledger(session_factory, MIB)


def test_optional_growth_is_bounded_by_disk_headroom_but_required_write_is_rejected(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _configure(monkeypatch, tmp_path)
    adapter_id = create_task(api_client, "issue198-disk-headroom")["id"]
    minimum = 64 * MIB
    set_policy(session_factory, min_free_space_bytes=minimum)
    monkeypatch.setattr(managed_input_upload, "_free_bytes", lambda _store: minimum + 32768)
    with session_factory() as session:
        state = managed_input_upload.begin_upload(
            session,
            adapter_id,
            original_filename="disk.txt",
            content_type="text/plain",
            store=store,
        )
    assert state.reserved_bytes == 32768
    with session_factory() as session:
        state = managed_input_upload.expand_upload_reservation(
            session,
            adapter_id,
            state.upload_session_id,
            state.reserved_bytes + 1,
            store=store,
            growth_bytes=managed_input_upload.RESERVATION_GROWTH_BYTES,
        )
    assert state.reserved_bytes == 65536
    _assert_ledger(session_factory, 0, 65536)
    monkeypatch.setattr(managed_input_upload, "_free_bytes", lambda _store: minimum)
    with session_factory() as session, pytest.raises(HTTPException) as rejected:
        managed_input_upload.expand_upload_reservation(
            session,
            adapter_id,
            state.upload_session_id,
            state.reserved_bytes + 1,
            store=store,
            growth_bytes=managed_input_upload.RESERVATION_GROWTH_BYTES,
        )
    assert rejected.value.detail["code"] == "artifact_store_low_watermark"
    _assert_ledger(session_factory, 0, 65536)
    with session_factory() as session:
        managed_input_upload.abort_upload(session, adapter_id, state.upload_session_id, store=store)
    _assert_ledger(session_factory, 0)
