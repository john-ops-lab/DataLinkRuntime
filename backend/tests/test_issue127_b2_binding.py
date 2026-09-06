"""Issue #127 B2 binding, retention, and lifecycle contract tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    AdapterSchedule,
    Execution,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputUploadReservation,
)
from dlr.control.schemas.input_config import AdapterInputConfigUpsert
from dlr.control.services import input_config as input_config_service
from dlr.control.services.schedule import scheduler_tick
from runtime_api_support import claim_execution
from test_adapters import save_version

FIXED_NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def create_task(api_client: TestClient, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_artifact(
    session_factory: sessionmaker[Session],
    adapter_id: int,
    filename: str,
    *,
    status: str = "STAGED",
    expires_at: datetime | None = FIXED_NOW + timedelta(hours=1),
) -> int:
    with session_factory.begin() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=adapter_id,
            upload_session_id=f"b2-session-{adapter_id}-{filename}",
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
            storage_key=f"b2-storage-{adapter_id}-{reservation.id:08d}",
            content_type="text/plain",
            size_bytes=8,
            sha256="a" * 64,
            status=status,
            retention_mode="system_default",
            expires_at=expires_at,
            created_at=FIXED_NOW,
        )
        session.add(artifact)
        session.flush()
        return artifact.id


def binding_rows(session_factory: sessionmaker[Session], adapter_id: int) -> list[tuple[int, int]]:
    with session_factory() as session:
        return [
            (row.artifact_id, row.ordinal)
            for row in session.scalars(
                select(AdapterInputArtifactBinding)
                .where(AdapterInputArtifactBinding.adapter_id == adapter_id)
                .order_by(AdapterInputArtifactBinding.ordinal)
            ).all()
        ]


def artifact_statuses(
    session_factory: sessionmaker[Session], artifact_ids: list[int]
) -> dict[int, str]:
    with session_factory() as session:
        return {
            artifact.id: str(artifact.status)
            for artifact in session.scalars(
                select(ManagedInputArtifact).where(ManagedInputArtifact.id.in_(artifact_ids))
            ).all()
        }


def test_binding_save_replaces_current_set_and_concretizes_retention(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_task(api_client, "b2-bind-replace")
    first = create_artifact(session_factory, adapter["id"], "first.txt")
    second = create_artifact(session_factory, adapter["id"], "second.txt")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [first, second],
            "retention": {"mode": "custom", "seconds": 7200},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert body["valid_for_run"] is True
    assert [artifact["id"] for artifact in body["artifacts"]] == [first, second]
    assert binding_rows(session_factory, adapter["id"]) == [(first, 0), (second, 1)]
    assert artifact_statuses(session_factory, [first, second]) == {
        first: "READY",
        second: "READY",
    }
    with session_factory() as session:
        artifacts = {
            artifact.id: artifact
            for artifact in session.scalars(
                select(ManagedInputArtifact).where(ManagedInputArtifact.id.in_([first, second]))
            ).all()
        }
        assert artifacts[first].expires_at == FIXED_NOW + timedelta(seconds=7200)
        assert artifacts[second].expires_at == FIXED_NOW + timedelta(seconds=7200)


def test_managed_files_limit_has_service_error_at_nine_and_accepts_eight(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_task(api_client, "b2-file-limit")
    artifact_ids = [
        create_artifact(session_factory, adapter["id"], f"limit-{index}.txt") for index in range(9)
    ]

    accepted = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": artifact_ids[:8],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["revision"] == 2
    assert binding_rows(session_factory, adapter["id"]) == [
        (artifact_id, ordinal) for ordinal, artifact_id in enumerate(artifact_ids[:8])
    ]

    rejected = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 2,
            "source_type": "managed_files",
            "artifact_ids": artifact_ids,
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"] == {
        "code": "input_invalid",
        "message": "At most eight managed input files may be bound",
        "params": {"reason": "managed_files_limit", "max_files": 8},
    }
    assert binding_rows(session_factory, adapter["id"]) == [
        (artifact_id, ordinal) for ordinal, artifact_id in enumerate(artifact_ids[:8])
    ]
    assert artifact_statuses(session_factory, artifact_ids) == {
        **{artifact_id: "READY" for artifact_id in artifact_ids[:8]},
        artifact_ids[8]: "STAGED",
    }
    with session_factory() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None and config.revision == 2


def test_nfc_casefold_name_conflict_has_zero_side_effects(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_task(api_client, "b2-name-conflict")
    composed = create_artifact(session_factory, adapter["id"], "caf\u00e9.txt")
    decomposed = create_artifact(session_factory, adapter["id"], "cafe\u0301.TXT")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [composed, decomposed],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "input_invalid"
    assert response.json()["detail"]["params"]["reason"] == "artifact_name_conflict"
    assert binding_rows(session_factory, adapter["id"]) == []
    assert artifact_statuses(session_factory, [composed, decomposed]) == {
        composed: "STAGED",
        decomposed: "STAGED",
    }
    with session_factory() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None and config.revision == 1


def test_old_revision_concurrent_save_has_one_commit_and_no_loser_side_effect(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_task(api_client, "b2-revision-race")
    first = create_artifact(session_factory, adapter["id"], "one.txt")
    second = create_artifact(session_factory, adapter["id"], "two.txt")
    barrier = threading.Barrier(2)
    results: list[tuple[str, str | None]] = []

    def save(artifact_id: int) -> None:
        with session_factory() as session:
            barrier.wait(timeout=5)
            try:
                input_config_service.upsert_input_config(
                    session,
                    adapter["id"],
                    AdapterInputConfigUpsert(
                        expected_revision=1,
                        source_type="managed_files",
                        artifact_ids=[artifact_id],
                        retention={"mode": "system_default", "seconds": None},
                    ),
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                results.append(("error", detail.get("code")))
            else:
                results.append(("ok", None))

    threads = [
        threading.Thread(target=save, args=(artifact_id,)) for artifact_id in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(results) == [("error", "input_config_revision_conflict"), ("ok", None)]
    with session_factory() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None and config.revision == 2
    rows = binding_rows(session_factory, adapter["id"])
    assert rows in ([(first, 0)], [(second, 0)])
    winner = rows[0][0]
    loser = second if winner == first else first
    assert artifact_statuses(session_factory, [winner, loser]) == {winner: "READY", loser: "STAGED"}


@pytest.mark.parametrize(
    ("mode", "seconds", "expected_expiry"),
    [
        ("system_default", None, FIXED_NOW + timedelta(seconds=86_400)),
        ("custom", 7200, FIXED_NOW + timedelta(seconds=7200)),
        ("manual_delete", None, None),
    ],
)
def test_retention_is_server_clocked_and_default_changes_do_not_rewrite(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    seconds: int | None,
    expected_expiry: datetime | None,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_task(api_client, f"b2-retention-{mode}")
    artifact_id = create_artifact(session_factory, adapter["id"], "retention.txt")
    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": mode, "seconds": seconds},
        },
    )
    assert response.status_code == 200, response.text
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        assert artifact.expires_at == expected_expiry

    current = api_client.get("/api/system/managed-input-settings").json()
    setting_fields = {
        "default_retention_seconds",
        "max_file_bytes",
        "platform_quota_bytes",
        "adapter_quota_bytes",
        "allow_manual_delete",
        "max_custom_retention_seconds",
        "min_free_space_bytes",
        "staged_ttl_seconds",
    }
    changed = api_client.put(
        "/api/system/managed-input-settings",
        json={field: current[field] for field in setting_fields}
        | {"default_retention_seconds": 172800},
    )
    assert changed.status_code == 200, changed.text
    with session_factory() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None and artifact.expires_at == expected_expiry


def test_replace_and_source_switch_preserve_old_blob_until_lifecycle_governance(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_task(api_client, "b2-explicit-replace")
    old = create_artifact(session_factory, adapter["id"], "old.txt")
    new = create_artifact(session_factory, adapter["id"], "new.txt")
    first = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [old],
            "retention": {"mode": "custom", "seconds": 7200},
        },
    )
    assert first.status_code == 200, first.text

    failed = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 2,
            "source_type": "managed_files",
            "artifact_ids": [new, 999999],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert failed.status_code == 404, failed.text
    assert artifact_statuses(session_factory, [old, new]) == {old: "READY", new: "STAGED"}
    assert binding_rows(session_factory, adapter["id"]) == [(old, 0)]

    switched = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 2,
            "source_type": "managed_files",
            "artifact_ids": [new],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert switched.status_code == 200, switched.text
    assert artifact_statuses(session_factory, [old, new]) == {
        old: "PENDING_DELETE",
        new: "READY",
    }
    assert binding_rows(session_factory, adapter["id"]) == [(new, 0)]
    with session_factory() as session:
        stored = {
            artifact.id: artifact
            for artifact in session.scalars(
                select(ManagedInputArtifact).where(ManagedInputArtifact.id.in_([old, new]))
            ).all()
        }
        assert stored[old].retention_mode == "custom"
        assert stored[old].expires_at == FIXED_NOW + timedelta(seconds=7200)
        assert stored[new].retention_mode == "system_default"
        assert stored[new].expires_at == FIXED_NOW + timedelta(seconds=86_400)

    switched_source = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 3,
            "source_type": "none",
        },
    )
    assert switched_source.status_code == 200, switched_source.text
    assert switched_source.json()["revision"] == 4
    assert switched_source.json()["retention"] == {
        "mode": "system_default",
        "seconds": None,
    }
    assert binding_rows(session_factory, adapter["id"]) == []
    assert artifact_statuses(session_factory, [old, new]) == {
        old: "PENDING_DELETE",
        new: "PENDING_DELETE",
    }


def test_active_execution_lock_and_lifecycle_keep_snapshot_immutable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    # Execution/Outbox use the real DB clock. The later lifecycle transition
    # receives its future clock explicitly, without aging a new Outbox row.
    adapter = create_task(api_client, "b2-lifecycle-lock")
    save_version(api_client, adapter["id"])
    artifact_id = create_artifact(session_factory, adapter["id"], "expiring.txt")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    snapshot = execution.json()["input_snapshot"]
    assert snapshot == {
        "source_type": "managed_files",
        "revision": 2,
        "artifacts": [
            {
                "ordinal": 0,
                "original_filename": "expiring.txt",
                "content_type": "text/plain",
                "size_bytes": 8,
                "sha256": "a" * 64,
            }
        ],
    }
    assert "storage_key" not in execution.text
    assert "upload_session_id" not in execution.text
    claimed = claim_execution(api_client, execution.json()["target_worker_id"])
    assert claimed.status_code == 200, claimed.text

    locked = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 2,
            "source_type": "managed_files",
            "artifact_ids": [],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert locked.status_code == 409, locked.text
    assert locked.json()["detail"]["code"] == "adapter_runtime_locked"

    with session_factory.begin() as session:
        session.add(
            AdapterSchedule(
                adapter_id=adapter["id"],
                cron="* * * * *",
                timezone="UTC",
                input={"stale": "legacy-mirror"},
                enabled=False,
            )
        )

    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.expires_at = FIXED_NOW - timedelta(seconds=1)
    with session_factory() as session:
        result = input_config_service.expire_current_bindings(session, adapter["id"], now=FIXED_NOW)
    assert result.revision == 3
    assert binding_rows(session_factory, adapter["id"]) == []
    assert artifact_statuses(session_factory, [artifact_id]) == {artifact_id: "PENDING_DELETE"}
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None and schedule.input is None
        current_execution = session.scalar(
            select(Execution).where(Execution.id == execution.json()["id"])
        )
        assert current_execution is not None
        assert current_execution.status == "running"
        assert current_execution.input_snapshot == snapshot


def test_lifecycle_noop_does_not_commit_callers_unrelated_changes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_task(api_client, "b2-lifecycle-noop-transaction")
    artifact_id = create_artifact(session_factory, adapter["id"], "stable.txt")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text

    with session_factory() as session:
        adapter_row = session.get(Adapter, adapter["id"])
        assert adapter_row is not None
        original_name = adapter_row.name
        adapter_row.name = "b2-uncommitted-lifecycle-change"

        result = input_config_service.reconcile_current_bindings(
            session, adapter["id"], now=FIXED_NOW
        )
        assert result.revision == 2
        session.rollback()

    with session_factory() as session:
        adapter_row = session.get(Adapter, adapter["id"])
        assert adapter_row is not None
        assert adapter_row.name == original_name


def test_schedule_managed_files_snapshot_survives_later_replacement(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_task(api_client, "b2-schedule-snapshot")
    save_version(api_client, adapter["id"])
    first = create_artifact(session_factory, adapter["id"], "scheduled.txt")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [first],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    saved_body = saved.json()
    switched = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert switched.status_code == 200, switched.text
    schedule = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={"enabled": True, "cron": "* * * * *", "timezone": "UTC"},
    )
    assert schedule.status_code == 200, schedule.text

    due = datetime.now(UTC) - timedelta(minutes=1)
    with session_factory.begin() as session:
        row = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert row is not None
        row.next_run_at = due
    tick_now = datetime.now(UTC)
    with session_factory() as session:
        assert scheduler_tick(session, now=tick_now) == 1
        execution = session.scalar(
            select(Execution)
            .where(Execution.adapter_id == adapter["id"], Execution.trigger == "schedule")
            .order_by(Execution.id.desc())
        )
        assert execution is not None
        snapshot = execution.input_snapshot
        assert snapshot["source_type"] == "managed_files"
        assert snapshot["revision"] == saved_body["revision"]
        assert snapshot["artifacts"][0]["original_filename"] == "scheduled.txt"
        assert "id" not in snapshot["artifacts"][0]
        assert "storage_key" not in snapshot["artifacts"][0]

    cancelled = api_client.post(f"/api/executions/{execution.id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    disabled = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={"enabled": False, "cron": "* * * * *", "timezone": "UTC"},
    )
    assert disabled.status_code == 200, disabled.text
    second = create_artifact(session_factory, adapter["id"], "replacement.txt")
    replaced = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": saved_body["revision"],
            "source_type": "managed_files",
            "artifact_ids": [second],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert replaced.status_code == 200, replaced.text
    fetched = api_client.get(f"/api/executions/{execution.id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["input_snapshot"] == snapshot


def test_legacy_schedule_input_uses_full_binding_transition_and_distinguishes_omission(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_task(api_client, "b2-legacy-schedule-transition")
    artifact_id = create_artifact(session_factory, adapter["id"], "legacy-switch.txt")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    switched = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert switched.status_code == 200, switched.text

    omitted = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={"enabled": False, "cron": "* * * * *", "timezone": "UTC"},
    )
    assert omitted.status_code == 200, omitted.text
    current = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert current.status_code == 200, current.text
    assert current.json()["source_type"] == "managed_files"
    assert current.json()["revision"] == 2
    assert binding_rows(session_factory, adapter["id"]) == [(artifact_id, 0)]
    assert artifact_statuses(session_factory, [artifact_id]) == {artifact_id: "READY"}

    explicit_null = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={
            "enabled": False,
            "cron": "* * * * *",
            "timezone": "UTC",
            "input": None,
        },
    )
    assert explicit_null.status_code == 200, explicit_null.text
    assert explicit_null.json()["input"] is None
    current = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert current.status_code == 200, current.text
    assert current.json()["source_type"] == "json"
    assert current.json()["json_value"] is None
    assert current.json()["revision"] == 3
    assert current.json()["artifacts"] == []
    assert binding_rows(session_factory, adapter["id"]) == []
    assert artifact_statuses(session_factory, [artifact_id]) == {artifact_id: "PENDING_DELETE"}

    omitted_again = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={"enabled": False, "cron": "*/5 * * * *", "timezone": "UTC"},
    )
    assert omitted_again.status_code == 200, omitted_again.text
    current = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert current.status_code == 200, current.text
    assert current.json()["source_type"] == "json"
    assert current.json()["json_value"] is None
    assert current.json()["revision"] == 3


def test_ready_artifact_delete_requires_current_revision_and_uses_binding_transition(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_task(api_client, "b2-ready-delete-revision")
    artifact_id = create_artifact(session_factory, adapter["id"], "ready-delete.txt")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2

    missing = api_client.delete(f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}")
    assert missing.status_code == 422
    assert missing.json()["detail"]["params"] == {"reason": "expected_revision_required"}
    stale = api_client.delete(
        f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}?expected_revision=1"
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "input_config_revision_conflict"
    assert binding_rows(session_factory, adapter["id"]) == [(artifact_id, 0)]
    assert artifact_statuses(session_factory, [artifact_id]) == {artifact_id: "READY"}

    deleted = api_client.delete(
        f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}?expected_revision=2"
    )
    assert deleted.status_code == 204, deleted.text
    current = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert current.status_code == 200, current.text
    assert current.json()["revision"] == 3
    assert current.json()["source_type"] == "managed_files"
    assert current.json()["artifacts"] == []
    assert binding_rows(session_factory, adapter["id"]) == []
    assert artifact_statuses(session_factory, [artifact_id]) == {artifact_id: "PENDING_DELETE"}
    repeated = api_client.delete(
        f"/api/adapters/{adapter['id']}/input-artifacts/{artifact_id}?expected_revision=2"
    )
    assert repeated.status_code == 204


def test_corrupt_artifact_uses_stable_reason_and_redacted_response(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_task(api_client, "b2-corrupt-reason")
    artifact_id = create_artifact(
        session_factory,
        adapter["id"],
        "corrupt.txt",
        status="READY",
    )
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.sha256 = "not-a-sha"
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["valid_for_run"] is False
    assert body["invalid_reason"] == "artifact_corrupt"
    assert "storage_key" not in saved.text
    assert "upload_session_id" not in saved.text


def test_ready_managed_files_are_runnable_and_invalid_states_expose_reason(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(input_config_service, "database_now", lambda _session: FIXED_NOW)
    adapter = create_task(api_client, "b2-validity")
    artifact_id = create_artifact(session_factory, adapter["id"], "valid.txt")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["valid_for_run"] is True
    with session_factory() as session:
        resolved = input_config_service.resolve_for_execution(session, adapter["id"])
        assert resolved.runtime_input is None
        assert resolved.source_type == "managed_files"
        assert resolved.snapshot["artifacts"][0]["original_filename"] == "valid.txt"

    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.status = ManagedInputArtifactStatus.STAGED
    invalid = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert invalid.status_code == 200, invalid.text
    assert invalid.json()["valid_for_run"] is False
    assert invalid.json()["invalid_reason"] == "artifact_not_ready"
