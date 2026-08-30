"""Issue #127 E0 real PostgreSQL Lease/GC versus Execution race probe.

This is deliberately run inside the retained Control container.  It creates
two private fixtures, uses two independent SQLAlchemy sessions and the actual
C0 services, and prints only safe IDs/status/count facts.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from dlr.control import db
from dlr.control.models import (
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    Execution,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputCapacity,
    ManagedInputUploadReservation,
)
from dlr.control.schemas.adapter import AdapterCreate, VersionCreate
from dlr.control.schemas.execution import ExecutionCreate
from dlr.control.schemas.worker import WorkerRegister
from dlr.control.services import adapter as adapter_service
from dlr.control.services import execution as execution_service
from dlr.control.services import input_config as input_config_service
from dlr.control.services import managed_input as managed_input_service
from dlr.control.services import managed_input_gc
from dlr.control.services import worker as worker_service
from dlr.control.services.artifact_store import LocalFileArtifactStore
from fastapi import HTTPException
from sqlalchemy import select

NOW = datetime.now(UTC)
SIZE = 8
PAYLOAD = b"payload!"
PAYLOAD_SHA256 = sha256(PAYLOAD).hexdigest()
RACE_TAG = os.environ.get("DLR_E0_RACE_TAG", "initial")


def make_fixture(
    name: str, *, expired: bool
) -> tuple[int, int, LocalFileArtifactStore]:
    fixture_name = f"{name} {RACE_TAG}"
    with db.SessionLocal() as session:
        adapter = adapter_service.create_adapter(
            session,
            AdapterCreate(
                name=fixture_name,
                description="Issue 127 E0 race fixture",
                language="python",
                adapter_type="task",
                timeout_seconds=60,
            ),
            owner_user_id=None,
        )
    with db.SessionLocal() as session:
        adapter_service.save_version(
            session,
            int(adapter.id),
            VersionCreate(code="def handle(context, input):\n    return input\n"),
        )

    store = LocalFileArtifactStore()
    with db.SessionLocal.begin() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=int(adapter.id),
            upload_session_id=f"e0-race-{adapter.id}",
            reserved_bytes=0,
            status="CONSUMED",
            expires_at=NOW + timedelta(hours=1),
            consumed_at=NOW,
        )
        session.add(reservation)
        session.flush()
        artifact = ManagedInputArtifact(
            adapter_id=int(adapter.id),
            created_by_user_id=None,
            upload_session_id=reservation.upload_session_id,
            upload_reservation_id=int(reservation.id),
            original_filename=f"{name}.txt",
            storage_key=f"{int(adapter.id):016x}{int(reservation.id):048x}",
            content_type="text/plain",
            size_bytes=SIZE,
            sha256=PAYLOAD_SHA256,
            status=(
                ManagedInputArtifactStatus.PENDING_DELETE
                if expired
                else ManagedInputArtifactStatus.READY
            ),
            retention_mode="system_default",
            expires_at=NOW - timedelta(seconds=1)
            if expired
            else NOW + timedelta(hours=1),
            created_at=NOW,
        )
        session.add(artifact)
        session.flush()
        config = session.scalar(
            select(AdapterInputConfig).where(
                AdapterInputConfig.adapter_id == int(adapter.id)
            )
        )
        assert config is not None
        config.source_type = "managed_files"
        config.json_value = None
        config.retention_mode = "system_default"
        config.retention_seconds = None
        config.revision = 2
        session.add(
            AdapterInputArtifactBinding(
                adapter_id=int(adapter.id),
                artifact_id=int(artifact.id),
                input_config_revision=2,
                ordinal=0,
            )
        )
        capacity = managed_input_service.get_capacity(session, for_update=True)
        capacity.actual_bytes += SIZE
        artifact_id = int(artifact.id)
    with store.put_part(artifact.storage_key) as part:
        part.write(PAYLOAD)
    store.commit(artifact.storage_key)
    return int(adapter.id), artifact_id, store


def capacity() -> int:
    with db.SessionLocal() as session:
        row = session.get(ManagedInputCapacity, 1)
        assert row is not None
        return int(row.actual_bytes)


def read_and_hash(store: LocalFileArtifactStore, storage_key: str) -> dict[str, object]:
    """Read the real published Blob and return only safe verification facts."""
    digest = sha256()
    read_bytes = 0
    with store.open(storage_key) as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
            read_bytes += len(chunk)
    return {
        "blob_read": True,
        "blob_read_bytes": read_bytes,
        "blob_sha256_matches": digest.hexdigest() == PAYLOAD_SHA256,
    }


def ensure_worker() -> None:
    """Make the probe reproducible on a fresh migrated database."""
    with db.SessionLocal() as session:
        worker_service.register_worker(
            session,
            WorkerRegister(
                name=f"issue127-e0-race-{RACE_TAG}",
                capabilities=["python"],
                protocol_version=2,
            ),
        )


def lease_first() -> dict[str, object]:
    adapter_id, artifact_id, store = make_fixture("E0 race lease first", expired=False)
    before = capacity()
    creator_reached_artifact = threading.Event()
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    errors: list[tuple[str, str]] = []
    real_database_now = input_config_service.database_now

    def pause_after_artifact_lock(session: object) -> datetime:
        if (
            threading.current_thread().name == "e0-lease-creator"
            and not creator_reached_artifact.is_set()
        ):
            creator_reached_artifact.set()
            barrier.wait(timeout=10)
        return real_database_now(session)  # type: ignore[arg-type]

    input_config_service.database_now = pause_after_artifact_lock  # type: ignore[assignment]

    def creator() -> None:
        try:
            with db.SessionLocal() as session:
                created = execution_service.create_execution(
                    session, adapter_id, ExecutionCreate()
                )
                outcomes["execution_id"] = int(created.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(("creator", str(exc)))
            barrier.abort()

    def gc_competitor() -> None:
        try:
            with db.SessionLocal() as session:
                if not creator_reached_artifact.wait(timeout=10):
                    raise AssertionError("creator did not reach Artifact lock")
                barrier.wait(timeout=10)
                observed = session.scalar(
                    select(ManagedInputArtifact)
                    .where(ManagedInputArtifact.id == artifact_id)
                    .with_for_update()
                )
                assert observed is not None
                outcomes["gc_observed_lease"] = (
                    managed_input_gc.has_active_artifact_lease(
                        session,
                        artifact_id,
                    )
                )
                observed.status = ManagedInputArtifactStatus.PENDING_DELETE
                session.commit()
                report = managed_input_gc.process_artifact_deletions(
                    session, store=store, now=NOW
                )
                outcomes["gc_claimed"] = int(report.claimed)
                outcomes["gc_deleted"] = int(report.deleted)
        except Exception as exc:  # noqa: BLE001
            errors.append(("gc", str(exc)))
            barrier.abort()

    creator_thread = threading.Thread(target=creator, name="e0-lease-creator")
    gc_thread = threading.Thread(target=gc_competitor, name="e0-lease-gc")
    creator_thread.start()
    gc_thread.start()
    creator_thread.join(timeout=30)
    gc_thread.join(timeout=30)
    input_config_service.database_now = real_database_now  # type: ignore[assignment]
    assert not creator_thread.is_alive() and not gc_thread.is_alive()
    assert not errors, errors
    with db.SessionLocal() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        execution = session.get(Execution, int(outcomes["execution_id"]))
        assert artifact is not None and execution is not None
        final_status = str(artifact.status)
        execution_status = str(execution.status)
        storage_key = artifact.storage_key
    blob_read = read_and_hash(store, storage_key)
    after = capacity()
    return {
        "adapter_id": adapter_id,
        "artifact_id": artifact_id,
        "execution_id": int(outcomes["execution_id"]),
        "creator_execution_status": execution_status,
        "gc_observed_active_lease": bool(outcomes.get("gc_observed_lease")),
        "gc_claimed": int(outcomes.get("gc_claimed", -1)),
        "gc_deleted": int(outcomes.get("gc_deleted", -1)),
        "artifact_status_after_race": final_status,
        "capacity_actual_before": before,
        "capacity_actual_after": after,
        "capacity_unchanged": before == after,
        "blob_still_present": store.stat(store_key(store, artifact_id)) is not None,
        **blob_read,
    }


def store_key(store: LocalFileArtifactStore, artifact_id: int) -> str:
    with db.SessionLocal() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        if artifact is not None:
            return artifact.storage_key
        # The row is retained in lease-first and is expected to exist here.
        reservation = session.scalar(
            select(ManagedInputUploadReservation).where(
                ManagedInputUploadReservation.adapter_id == artifact_id
            )
        )
        return f"missing-{artifact_id}-{getattr(reservation, 'id', 0)}"


def no_governance_control() -> dict[str, object]:
    """Prove the ordinary READY fixture is runnable before governance."""
    adapter_id, artifact_id, store = make_fixture(
        "E0 race no governance", expired=False
    )
    with db.SessionLocal() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        blob_read = read_and_hash(store, artifact.storage_key)
    with db.SessionLocal() as session:
        execution = execution_service.create_execution(
            session, adapter_id, ExecutionCreate()
        )
        return {
            "adapter_id": adapter_id,
            "artifact_id": artifact_id,
            "execution_id": int(execution.id),
            "execution_status": str(execution.status),
            **blob_read,
        }


def governance_first() -> dict[str, object]:
    adapter_id, artifact_id, store = make_fixture(
        "E0 race governance first", expired=False
    )
    before = capacity()
    with db.SessionLocal() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        storage_key = artifact.storage_key

    governance_committed = threading.Event()
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    errors: list[tuple[str, str]] = []

    def creator() -> None:
        try:
            barrier.wait(timeout=10)
            if not governance_committed.wait(timeout=10):
                raise AssertionError("governance did not commit before create")
            with db.SessionLocal() as session:
                try:
                    execution_service.create_execution(
                        session, adapter_id, ExecutionCreate()
                    )
                except HTTPException as exc:
                    detail = exc.detail
                    assert isinstance(detail, dict)
                    outcomes["create_failure_status"] = int(exc.status_code)
                    outcomes["create_failure_code"] = str(detail["code"])
                    session.rollback()
                else:  # pragma: no cover - race assertion
                    raise AssertionError(
                        "governance-first create unexpectedly succeeded"
                    )
        except Exception as exc:  # noqa: BLE001
            errors.append(("creator", str(exc)))
            barrier.abort()

    def gc_competitor() -> None:
        try:
            barrier.wait(timeout=10)
            with db.SessionLocal() as session:
                outcomes.update(read_and_hash(store, storage_key))
                input_config_service.remove_ready_artifact(
                    session,
                    adapter_id,
                    artifact_id,
                    expected_revision=2,
                )
                outcomes["governance_committed"] = True
                claim = managed_input_gc.claim_artifact_deletion(
                    session,
                    artifact_id,
                    now=NOW,
                    force=True,
                )
                assert claim is not None
                outcomes["gc_claimed"] = True
                store.delete(claim.storage_key)
                assert managed_input_gc.finalize_artifact_deletion(
                    session,
                    claim,
                    succeeded=True,
                    now=NOW,
                )
                governance_committed.set()
        except Exception as exc:  # noqa: BLE001
            errors.append(("gc", str(exc)))
            governance_committed.set()
            barrier.abort()

    creator_thread = threading.Thread(target=creator, name="e0-governance-creator")
    gc_thread = threading.Thread(target=gc_competitor, name="e0-governance-gc")
    creator_thread.start()
    gc_thread.start()
    creator_thread.join(timeout=30)
    gc_thread.join(timeout=30)
    assert not creator_thread.is_alive() and not gc_thread.is_alive()
    assert not errors, errors
    with db.SessionLocal() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        execution = session.scalar(
            select(Execution).where(Execution.adapter_id == adapter_id).limit(1)
        )
        assert artifact is not None
        final_status = str(artifact.status)
        execution_exists = execution is not None
    after = capacity()
    return {
        "adapter_id": adapter_id,
        "artifact_id": artifact_id,
        "governance_committed": bool(outcomes.get("governance_committed")),
        "gc_claimed": bool(outcomes.get("gc_claimed")),
        "create_failure_status": outcomes.get("create_failure_status"),
        "create_failure_code": outcomes.get("create_failure_code"),
        "artifact_status_after_race": final_status,
        "execution_exists": execution_exists,
        "capacity_actual_before": before,
        "capacity_actual_after": after,
        "capacity_released": after == before - SIZE,
        "blob_deleted": store.stat(storage_key) is None,
        "blob_read": bool(outcomes.get("blob_read")),
        "blob_read_bytes": int(outcomes.get("blob_read_bytes", -1)),
        "blob_sha256_matches": bool(outcomes.get("blob_sha256_matches")),
    }


def main() -> None:
    ensure_worker()
    lease = lease_first()
    control = no_governance_control()
    governance = governance_first()
    result = {
        "schema": "issue127-e0-real-postgres-race-v1",
        "lease_first": lease,
        "no_governance_control": control,
        "governance_first": governance,
        "machine_gate": (
            lease["creator_execution_status"] == "pending"
            and lease["gc_observed_active_lease"]
            and lease["gc_claimed"] == 0
            and lease["gc_deleted"] == 0
            and lease["artifact_status_after_race"] == "PENDING_DELETE"
            and lease["capacity_unchanged"]
            and lease["blob_still_present"]
            and lease["blob_read"]
            and lease["blob_read_bytes"] == SIZE
            and lease["blob_sha256_matches"]
            and control["execution_status"] == "pending"
            and control["blob_read"]
            and control["blob_read_bytes"] == SIZE
            and control["blob_sha256_matches"]
            and governance["governance_committed"]
            and governance["gc_claimed"]
            and governance["create_failure_status"] == 422
            and governance["create_failure_code"] == "input_invalid"
            and governance["artifact_status_after_race"] == "DELETED"
            and not governance["execution_exists"]
            and governance["capacity_released"]
            and governance["blob_deleted"]
            and governance["blob_read"]
            and governance["blob_read_bytes"] == SIZE
            and governance["blob_sha256_matches"]
        ),
        "human_acceptance": "待人工验收",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
