"""Tests for the M1 Adapter management API against real PostgreSQL."""

import threading
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.schemas.adapter import AdapterCreate, VersionCreate
from dlr.control.services.adapter import create_adapter as service_create_adapter
from dlr.control.services.adapter import save_version as service_save_version
from runtime_api_support import (
    claim_execution,
    mark_broker_ready,
    ready_registration,
    report_attempt,
)

STARTER_CODE = "def handle(context, input):\n    return input\n"
WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def create_adapter(client: TestClient, name: str = "example-adapter", **extra: Any) -> dict:
    payload: dict[str, Any] = {"name": name, "description": extra.pop("description", "")}
    payload.update(extra)
    payload.setdefault("language", "python")
    payload.setdefault("adapter_type", "task")
    response = client.post("/api/adapters", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def save_version(
    client: TestClient,
    adapter_id: int,
    code: str = STARTER_CODE,
    requirements: str = "",
    runtime_config: Any = None,
) -> dict:
    adapter = client.get(f"/api/adapters/{adapter_id}").json()
    if adapter.get("runtime_worker_id") is None:
        workers = client.get("/api/workers").json()
        compatible = [
            worker
            for worker in workers
            if worker["status"] == "online" and adapter["language"] in worker["capabilities"]
        ]
        if not compatible:
            response = client.post(
                "/api/workers/register",
                json={
                    **ready_registration("worker-1", [adapter["language"]]),
                    "name": "worker-1",
                    "capabilities": [adapter["language"]],
                },
                headers=WORKER_HEADERS,
            )
            assert response.status_code == 200, response.text
            compatible = [response.json()]
        response = client.patch(
            f"/api/adapters/{adapter_id}",
            json={"runtime_worker_id": compatible[0]["id"]},
        )
        assert response.status_code == 200, response.text
    mark_broker_ready()
    payload: dict[str, Any] = {"code": code, "requirements": requirements}
    if runtime_config is not None:
        payload["runtime_config"] = runtime_config
    response = client.post(f"/api/adapters/{adapter_id}/versions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def pass_publish_gate(
    client: TestClient, adapter_id: int, version_id: int, worker_name: str = "gate-worker"
) -> dict:
    """Compatibility helper for tests that need one completed Manual run."""
    register = client.post(
        "/api/workers/register",
        json=ready_registration(worker_name, ["python"]),
        headers=WORKER_HEADERS,
    )
    assert register.status_code == 200, register.text
    worker = register.json()
    mark_broker_ready()
    patch = client.patch(f"/api/adapters/{adapter_id}", json={"runtime_worker_id": worker["id"]})
    assert patch.status_code == 200, patch.text
    execution = client.post(f"/api/adapters/{adapter_id}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = claim_execution(client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    result = report_attempt(client, worker["id"], execution.json()["id"], {"status": "succeeded"})
    assert result.status_code == 200, result.text
    return worker


# --- Adapter CRUD -----------------------------------------------------------


def test_create_adapter_success(api_client: TestClient) -> None:
    body = create_adapter(api_client, name="cmdb-sync", description="sync cmdb")
    assert body["name"] == "cmdb-sync"
    assert body["description"] == "sync cmdb"
    assert body["language"] == "python"
    assert body["adapter_type"] == "task"
    assert body["run_mode"] == "manual"
    assert body["latest_version_id"] is None
    assert body["created_at"]
    assert body["updated_at"]

    listed = api_client.get("/api/adapters").json()
    assert [adapter["id"] for adapter in listed] == [body["id"]]


def test_create_adapter_trims_name(api_client: TestClient) -> None:
    body = create_adapter(api_client, name="  padded  ")
    assert body["name"] == "padded"


def test_create_adapter_blank_name_rejected(api_client: TestClient) -> None:
    for name in ("", "   "):
        response = api_client.post("/api/adapters", json={"name": name})
        assert response.status_code == 422


def test_create_adapter_invalid_language_rejected(api_client: TestClient) -> None:
    response = api_client.post("/api/adapters", json={"name": "x", "language": "ruby"})
    assert response.status_code == 422


def test_create_adapter_accepts_all_supported_languages(api_client: TestClient) -> None:
    for language in ("python", "javascript", "java"):
        response = api_client.post(
            "/api/adapters",
            json={
                "name": f"adapter-{language}",
                "language": language,
                "adapter_type": "task",
            },
        )
        assert response.status_code == 201
        assert response.json()["language"] == language


def test_create_adapter_requires_language_and_type(api_client: TestClient) -> None:
    assert api_client.post("/api/adapters", json={"name": "missing-both"}).status_code == 422
    assert (
        api_client.post(
            "/api/adapters",
            json={"name": "missing-type", "language": "python"},
        ).status_code
        == 422
    )


def test_create_adapter_accepts_task_and_webhook(api_client: TestClient) -> None:
    for adapter_type in ("task", "webhook"):
        body = create_adapter(api_client, name=adapter_type, adapter_type=adapter_type)
        assert body["adapter_type"] == adapter_type


def test_create_adapter_duplicate_name_conflict(api_client: TestClient) -> None:
    create_adapter(api_client, name="dup")
    response = api_client.post(
        "/api/adapters",
        json={"name": "dup", "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_name_conflict"


def test_create_adapter_name_conflict_ignores_surrounding_whitespace(
    api_client: TestClient,
) -> None:
    create_adapter(api_client, name="padded")
    response = api_client.post(
        "/api/adapters",
        json={"name": "  padded  ", "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_name_conflict"


def test_create_adapter_reuses_soft_deleted_name(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="reusable")
    assert api_client.delete(f"/api/adapters/{created['id']}").status_code == 204

    response = api_client.post(
        "/api/adapters",
        json={"name": "reusable", "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "reusable"


def test_get_adapter(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    response = api_client.get(f"/api/adapters/{created['id']}")
    assert response.status_code == 200
    assert response.json()["name"] == created["name"]


def test_get_adapter_not_found(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters/99999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "adapter_not_found"


def test_patch_adapter_updates_metadata(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="before")
    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={"name": "after", "description": "new description"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "after"
    assert body["description"] == "new description"
    assert body["updated_at"] >= created["updated_at"]


def test_task_run_mode_can_change_while_unlocked(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="scheduled-task")

    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={"run_mode": "schedule"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["run_mode"] == "schedule"


def test_webhook_rejects_task_run_mode_change(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="hook", adapter_type="webhook")

    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={"run_mode": "schedule"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_type_mismatch"


def test_task_run_mode_rejects_null(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="null-mode")

    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={"run_mode": None},
    )

    assert response.status_code == 422
    assert api_client.get(f"/api/adapters/{created['id']}").json()["run_mode"] == "manual"


# --- M5.5.11 single-run execution timeout -----------------------------------


def test_create_adapter_timeout_defaults_to_300_seconds(api_client: TestClient) -> None:
    body = create_adapter(api_client, name="timeout-default")
    assert body["timeout_seconds"] == 300


def test_create_adapter_accepts_custom_timeout(api_client: TestClient) -> None:
    body = create_adapter(api_client, name="timeout-custom", timeout_seconds=600)
    assert body["timeout_seconds"] == 600


def test_create_adapter_rejects_out_of_range_timeout(api_client: TestClient) -> None:
    for bad in (0, -1, 86401):
        response = api_client.post(
            "/api/adapters",
            json={
                "name": f"timeout-bad-{bad}",
                "language": "python",
                "adapter_type": "task",
                "timeout_seconds": bad,
            },
        )
        assert response.status_code == 422, bad


def test_patch_adapter_updates_timeout_while_unlocked(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="timeout-patch")
    assert created["timeout_seconds"] == 300

    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={"timeout_seconds": 1800},
    )
    assert response.status_code == 200, response.text
    assert response.json()["timeout_seconds"] == 1800


def test_patch_adapter_timeout_bounds_and_null_rejected(api_client: TestClient) -> None:
    created = create_adapter(api_client, name="timeout-bounds")
    for bad in (0, 86401, None):
        response = api_client.patch(
            f"/api/adapters/{created['id']}",
            json={"timeout_seconds": bad},
        )
        assert response.status_code == 422, bad
    assert api_client.get(f"/api/adapters/{created['id']}").json()["timeout_seconds"] == 300


def test_patch_adapter_timeout_rejected_while_runtime_locked(api_client: TestClient) -> None:
    """An enabled Schedule locks the timeout exactly like the runtime Worker."""
    created = create_adapter(api_client, name="timeout-locked")
    save_version(api_client, created["id"])
    mode = api_client.patch(f"/api/adapters/{created['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    configured = api_client.put(
        f"/api/adapters/{created['id']}/schedule",
        json={"enabled": True, "cron": "*/5 * * * *", "timezone": "UTC", "input": {}},
    )
    assert configured.status_code == 200, configured.text

    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={"timeout_seconds": 600},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_runtime_locked"
    assert api_client.get(f"/api/adapters/{created['id']}").json()["timeout_seconds"] == 300


def test_clone_copies_timeout_seconds(api_client: TestClient) -> None:
    source = create_adapter(api_client, name="timeout-source", timeout_seconds=3600)
    save_version(api_client, source["id"])

    cloned = api_client.post(f"/api/adapters/{source['id']}/clone", json={"name": "copy"})
    assert cloned.status_code == 201, cloned.text
    assert cloned.json()["timeout_seconds"] == 3600


def test_patch_adapter_name_conflict(api_client: TestClient) -> None:
    create_adapter(api_client, name="taken")
    other = create_adapter(api_client, name="other")
    response = api_client.patch(f"/api/adapters/{other['id']}", json={"name": "taken"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_name_conflict"


def test_patch_adapter_can_rename_to_a_soft_deleted_name(api_client: TestClient) -> None:
    retired = create_adapter(api_client, name="retired")
    assert api_client.delete(f"/api/adapters/{retired['id']}").status_code == 204
    third = create_adapter(api_client, name="third")

    response = api_client.patch(f"/api/adapters/{third['id']}", json={"name": "retired"})
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "retired"


def test_patch_adapter_cannot_change_forbidden_fields(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])
    # Immutable/derived fields are rejected instead of silently ignored.
    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={
            "language": "javascript",
            "latest_version_id": None,
            "published_version_id": version["id"],
        },
    )
    assert response.status_code == 422
    body = api_client.get(f"/api/adapters/{created['id']}").json()
    assert body["language"] == "python"
    assert body["latest_version_id"] == version["id"]


def test_delete_adapter_permanently_removes_versions(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])

    response = api_client.delete(f"/api/adapters/{created['id']}")
    assert response.status_code == 204

    deleted = api_client.get(f"/api/adapters/{created['id']}")
    assert deleted.status_code == 404
    kept_version = api_client.get(f"/api/adapters/{created['id']}/versions/{version['id']}")
    assert kept_version.status_code == 404


def test_delete_adapter_not_found(api_client: TestClient) -> None:
    assert api_client.delete("/api/adapters/99999").status_code == 404


# --- Save new version -------------------------------------------------------


def test_save_first_version_gets_seq_1_and_becomes_latest(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"], runtime_config={"batch": 10})

    assert version["seq"] == 1
    assert version["code"] == STARTER_CODE
    assert version["runtime_config"] == {"batch": 10}

    adapter = api_client.get(f"/api/adapters/{created['id']}").json()
    assert adapter["latest_version_id"] == version["id"]


def test_consecutive_saves_increment_seq_and_update_latest(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    first = save_version(api_client, created["id"], code="v1")
    second = save_version(api_client, created["id"], code="v2", requirements="requests")

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert second["requirements"] == "requests"

    adapter = api_client.get(f"/api/adapters/{created['id']}").json()
    assert adapter["latest_version_id"] == second["id"]


def test_save_version_to_unknown_adapter(api_client: TestClient) -> None:
    response = api_client.post("/api/adapters/99999/versions", json={"code": STARTER_CODE})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "adapter_not_found"


def test_save_version_blank_code_rejected(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    response = api_client.post(f"/api/adapters/{created['id']}/versions", json={"code": "   "})
    assert response.status_code == 422


def test_save_version_runtime_config_must_be_object(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    for bad in ([1, 2], "text", 5, None):
        response = api_client.post(
            f"/api/adapters/{created['id']}/versions",
            json={"code": STARTER_CODE, "runtime_config": bad},
        )
        assert response.status_code == 422, bad


def test_versions_are_immutable_through_api(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])
    path = f"/api/adapters/{created['id']}/versions/{version['id']}"
    # No update or delete endpoints exist for versions.
    assert api_client.patch(path, json={"code": "changed"}).status_code == 405
    assert api_client.put(path, json={"code": "changed"}).status_code == 405
    assert api_client.delete(path).status_code == 405
    assert api_client.get(path).json()["code"] == STARTER_CODE


def test_list_versions_sorted_desc(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    save_version(api_client, created["id"], code="v1")
    save_version(api_client, created["id"], code="v2")
    save_version(api_client, created["id"], code="v3")

    versions = api_client.get(f"/api/adapters/{created['id']}/versions").json()
    assert [version["seq"] for version in versions] == [3, 2, 1]
    assert all(set(version) == {"id", "adapter_id", "seq", "created_at"} for version in versions)


def test_version_detail(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"], requirements="requests==2.32.0")

    detail = api_client.get(f"/api/adapters/{created['id']}/versions/{version['id']}").json()
    assert detail["code"] == STARTER_CODE
    assert detail["requirements"] == "requests==2.32.0"
    assert detail["runtime_config"] == {}


def test_version_of_other_adapter_not_found(api_client: TestClient) -> None:
    first = create_adapter(api_client, name="first")
    second = create_adapter(api_client, name="second")
    version = save_version(api_client, first["id"])

    response = api_client.get(f"/api/adapters/{second['id']}/versions/{version['id']}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "version_not_found"


# --- Removed Publish / Production API --------------------------------------


def test_publish_and_production_routes_are_removed(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])
    assert (
        api_client.post(
            f"/api/adapters/{created['id']}/versions/{version['id']}/publish"
        ).status_code
        == 404
    )
    assert api_client.post(f"/api/adapters/{created['id']}/production/start").status_code == 404
    assert api_client.post(f"/api/adapters/{created['id']}/unpublish").status_code == 404


def test_clone_copies_task_runtime_and_schedule_configuration_disabled(
    api_client: TestClient,
) -> None:
    source = create_adapter(api_client, name="source")
    save_version(
        api_client,
        source["id"],
        code="def handle(context, input):\n    return {'source': input}\n",
        requirements="requests==2.32.4",
        runtime_config={"timeout": 30},
    )
    mode = api_client.patch(
        f"/api/adapters/{source['id']}",
        json={"run_mode": "schedule"},
    )
    assert mode.status_code == 200, mode.text
    configured = api_client.put(
        f"/api/adapters/{source['id']}/schedule",
        json={
            "enabled": True,
            "cron": "*/5 * * * *",
            "timezone": "Asia/Shanghai",
            "input": {"kind": "sync"},
        },
    )
    assert configured.status_code == 200, configured.text

    cloned = api_client.post(
        f"/api/adapters/{source['id']}/clone",
        json={"name": "source-copy"},
    )

    assert cloned.status_code == 201, cloned.text
    body = cloned.json()
    assert body["run_mode"] == "schedule"
    assert body["runtime_worker_id"] == mode.json()["runtime_worker_id"]
    assert body["running_execution_id"] is None
    versions = api_client.get(f"/api/adapters/{body['id']}/versions").json()
    assert [version["seq"] for version in versions] == [1]
    detail = api_client.get(f"/api/adapters/{body['id']}/versions/{versions[0]['id']}").json()
    assert detail["requirements"] == "requests==2.32.4"
    assert detail["runtime_config"] == {"timeout": 30}
    schedule = api_client.get(f"/api/adapters/{body['id']}/schedule").json()
    assert schedule == {
        "adapter_id": body["id"],
        "enabled": False,
        "cron": "*/5 * * * *",
        "timezone": "Asia/Shanghai",
        "input": {"kind": "sync"},
        "next_run_at": None,
        "last_blocked_reason": None,
        "last_blocked_detail": None,
        "last_blocked_at": None,
        "last_processed_due_at": None,
        "misfire_policy": "coalesce_latest",
        "max_catchup_count": 100,
        "max_catchup_age_seconds": 86400,
        "recent_outcomes": [],
        "updated_at": schedule["updated_at"],
    }


# --- Concurrency contract ---------------------------------------------------


def test_concurrent_saves_keep_seq_unique_and_latest_correct(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Two independent sessions saving the same Adapter at the same moment.

    Verifies the load-bearing row-lock contract of save_version: the commits
    are serialized, so seq values are exactly {1, 2} (no duplicate and no
    unique violation), and latest ends up pointing at the seq=2 version.
    """
    created = create_adapter(api_client, name="concurrent-save")
    adapter_id = created["id"]
    worker_response = api_client.post(
        "/api/workers/register",
        json=ready_registration("concurrent-save-worker", ["python"]),
        headers=WORKER_HEADERS,
    )
    assert worker_response.status_code == 200
    assert (
        api_client.patch(
            f"/api/adapters/{adapter_id}",
            json={"runtime_worker_id": worker_response.json()["id"]},
        ).status_code
        == 200
    )

    start = threading.Barrier(2)
    saved: list[tuple[str, int, int]] = []  # (tag, version_id, seq)
    errors: list[BaseException] = []

    def worker(tag: str) -> None:
        session = session_factory()
        try:
            start.wait(timeout=5)
            version = service_save_version(
                session,
                adapter_id,
                VersionCreate(code=f"# saved by {tag}\n"),
            )
            saved.append((tag, version.id, version.seq))
        except BaseException as exc:  # noqa: BLE001 - collect to assert below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(seq for _, _, seq in saved) == [1, 2]

    detail = api_client.get(f"/api/adapters/{adapter_id}").json()
    seq_by_id = {version_id: seq for _, version_id, seq in saved}
    assert seq_by_id[detail["latest_version_id"]] == 2

    listed = api_client.get(f"/api/adapters/{adapter_id}/versions").json()
    assert [version["seq"] for version in listed] == [2, 1]


def test_concurrent_create_same_name_only_one_succeeds(
    session_factory: sessionmaker[Session],
) -> None:
    """Two independent sessions creating the same name at the same moment.

    The service pre-check passes in both sessions; the partial unique index
    (active Adapters only) is the final defense and exactly one commit wins,
    the loser is mapped to the stable 409 adapter_name_conflict.
    """
    from fastapi import HTTPException

    start = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(tag: str) -> None:
        session = session_factory()
        try:
            start.wait(timeout=5)
            created = service_create_adapter(
                session,
                AdapterCreate(
                    name="race-name",
                    description="",
                    language="python",
                    adapter_type="task",
                ),
            )
            outcomes.append(f"{tag}:ok:{created.id}")
        except HTTPException as exc:
            outcomes.append(f"{tag}:{exc.detail.get('code')}")
        except BaseException as exc:  # noqa: BLE001 - collect to assert below
            outcomes.append(f"{tag}:error:{type(exc).__name__}")
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    successes = [entry for entry in outcomes if ":ok:" in entry]
    conflicts = [entry for entry in outcomes if ":adapter_name_conflict" in entry]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert all(":error:" not in entry for entry in outcomes)
