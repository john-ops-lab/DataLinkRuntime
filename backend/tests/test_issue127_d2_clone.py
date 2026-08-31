"""Issue #127 D2: Adapter clone keeps input policy but never clones file assets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    Execution,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    ManagedInputUploadReservation,
)
from dlr.control.services.artifact_store import LocalFileArtifactStore
from test_adapters import save_version

FIXED_NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def create_task(api_client: TestClient, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_ready_artifact(
    session_factory: sessionmaker[Session], adapter_id: int
) -> tuple[int, str]:
    """Create one DB Artifact row and return its id plus opaque Blob key."""
    with session_factory.begin() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=adapter_id,
            upload_session_id=f"d2-clone-{adapter_id}",
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
            original_filename="source.csv",
            storage_key=f"{reservation.id:064x}",
            content_type="text/csv",
            size_bytes=12,
            sha256="c" * 64,
            status="STAGED",
            retention_mode="system_default",
            expires_at=FIXED_NOW + timedelta(hours=1),
            created_at=FIXED_NOW,
        )
        session.add(artifact)
        session.flush()
        return artifact.id, artifact.storage_key


def test_managed_files_clone_does_not_reuse_blob_artifact_binding_or_lease(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store_root = tmp_path / "artifact-store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    store = LocalFileArtifactStore(store_root)

    source = create_task(api_client, "d2-clone-source")
    source_id = source["id"]
    save_version(api_client, source_id)
    artifact_id, storage_key = create_ready_artifact(session_factory, source_id)
    blob = b"source,blob\n"
    store.object_path(storage_key).write_bytes(blob)

    saved = api_client.put(
        f"/api/adapters/{source_id}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "custom", "seconds": 7200},
        },
    )
    assert saved.status_code == 200, saved.text

    switched = api_client.patch(f"/api/adapters/{source_id}", json={"run_mode": "schedule"})
    assert switched.status_code == 200, switched.text
    schedule = api_client.put(
        f"/api/adapters/{source_id}/schedule",
        json={
            "enabled": True,
            "cron": "0 9 * * *",
            "timezone": "UTC",
        },
    )
    assert schedule.status_code == 200, schedule.text

    # Keep a real active Lease on the source while cloning.  The clone must
    # not transfer this authorization or make a second copy of the Blob.
    execution = api_client.post(f"/api/adapters/{source_id}/executions", json={})
    assert execution.status_code == 202, execution.text
    execution_id = execution.json()["id"]

    cloned = api_client.post(f"/api/adapters/{source_id}/clone", json={"name": "d2-clone-copy"})
    assert cloned.status_code == 201, cloned.text
    clone_id = cloned.json()["id"]

    clone_config = api_client.get(f"/api/adapters/{clone_id}/input-config")
    assert clone_config.status_code == 200, clone_config.text
    assert clone_config.json()["source_type"] == "managed_files"
    assert clone_config.json()["retention"] == {"mode": "custom", "seconds": 7200}
    assert clone_config.json()["artifacts"] == []

    clone_schedule = api_client.get(f"/api/adapters/{clone_id}/schedule")
    assert clone_schedule.status_code == 200, clone_schedule.text
    assert clone_schedule.json()["enabled"] is False
    assert clone_schedule.json()["next_run_at"] is None

    with session_factory() as session:
        source_artifact = session.get(ManagedInputArtifact, artifact_id)
        assert source_artifact is not None
        assert source_artifact.adapter_id == source_id
        assert session.get(AdapterInputConfig, source_id) is not None
        source_bindings = session.scalars(
            select(AdapterInputArtifactBinding).where(
                AdapterInputArtifactBinding.adapter_id == source_id
            )
        ).all()
        source_leases = session.scalars(
            select(ExecutionInputArtifactLease).where(
                ExecutionInputArtifactLease.execution_id == execution_id
            )
        ).all()
        assert [(row.artifact_id, row.ordinal) for row in source_bindings] == [(artifact_id, 0)]
        assert [(row.artifact_id, row.ordinal) for row in source_leases] == [(artifact_id, 0)]
        assert (
            session.scalars(
                select(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == clone_id)
            ).all()
            == []
        )
        assert (
            session.scalars(
                select(AdapterInputArtifactBinding).where(
                    AdapterInputArtifactBinding.adapter_id == clone_id
                )
            ).all()
            == []
        )
        assert (
            session.scalars(
                select(ExecutionInputArtifactLease)
                .join(Execution, Execution.id == ExecutionInputArtifactLease.execution_id)
                .where(Execution.adapter_id == clone_id)
            ).all()
            == []
        )

    # The source physical Blob remains the only object; cloning did not copy,
    # rename, or otherwise create a second object in the ArtifactStore.
    assert store.object_path(storage_key).read_bytes() == blob
    object_files = [path for path in (store_root / "objects").rglob("*") if path.is_file()]
    assert object_files == [store.object_path(storage_key)]
