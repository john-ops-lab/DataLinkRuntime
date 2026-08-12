"""Tests for the M1 Adapter management API against real PostgreSQL."""

import threading
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.schemas.adapter import VersionCreate
from dlr.control.services.adapter import save_version as service_save_version

STARTER_CODE = "def handle(context, input):\n    return input\n"
WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def create_adapter(client: TestClient, name: str = "example-adapter", **extra: Any) -> dict:
    payload: dict[str, Any] = {"name": name, "description": extra.pop("description", "")}
    payload.update(extra)
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
    payload: dict[str, Any] = {"code": code, "requirements": requirements}
    if runtime_config is not None:
        payload["runtime_config"] = runtime_config
    response = client.post(f"/api/adapters/{adapter_id}/versions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def pass_publish_gate(
    client: TestClient, adapter_id: int, version_id: int, worker_name: str = "gate-worker"
) -> dict:
    """Satisfy the M3.2 publish gate: configure the production Worker and
    run one succeeded test of the target version on it."""
    register = client.post(
        "/api/workers/register",
        json={"name": worker_name, "capabilities": ["python"]},
        headers=WORKER_HEADERS,
    )
    assert register.status_code == 200, register.text
    worker = register.json()
    patch = client.patch(f"/api/adapters/{adapter_id}", json={"production_worker_id": worker["id"]})
    assert patch.status_code == 200, patch.text
    execution = client.post(
        f"/api/adapters/{adapter_id}/executions", json={"version_id": version_id}
    )
    assert execution.status_code == 202, execution.text
    claimed = client.post(
        f"/api/workers/{worker['id']}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )
    assert claimed.status_code == 200, claimed.text
    result = client.post(
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}/result",
        json={"status": "succeeded"},
        headers=WORKER_HEADERS,
    )
    assert result.status_code == 200, result.text
    return worker


# --- Adapter CRUD -----------------------------------------------------------


def test_create_adapter_success(api_client: TestClient) -> None:
    body = create_adapter(api_client, name="cmdb-sync", description="sync cmdb")
    assert body["name"] == "cmdb-sync"
    assert body["description"] == "sync cmdb"
    assert body["language"] == "python"
    assert body["latest_version_id"] is None
    assert body["published_version_id"] is None
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
    response = api_client.post("/api/adapters", json={"name": "x", "language": "javascript"})
    assert response.status_code == 422


def test_create_adapter_language_defaults_to_python(api_client: TestClient) -> None:
    response = api_client.post("/api/adapters", json={"name": "no-language"})
    assert response.status_code == 201
    assert response.json()["language"] == "python"


def test_create_adapter_duplicate_name_conflict(api_client: TestClient) -> None:
    create_adapter(api_client, name="dup")
    response = api_client.post("/api/adapters", json={"name": "dup"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_name_conflict"


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


def test_patch_adapter_name_conflict(api_client: TestClient) -> None:
    create_adapter(api_client, name="taken")
    other = create_adapter(api_client, name="other")
    response = api_client.patch(f"/api/adapters/{other['id']}", json={"name": "taken"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_name_conflict"


def test_patch_adapter_cannot_change_forbidden_fields(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])
    # Foreign fields are ignored by the PATCH schema; pointers stay untouched.
    response = api_client.patch(
        f"/api/adapters/{created['id']}",
        json={
            "language": "javascript",
            "latest_version_id": None,
            "published_version_id": version["id"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["language"] == "python"
    assert body["latest_version_id"] == version["id"]
    assert body["published_version_id"] is None


def test_delete_adapter_removes_versions(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])

    response = api_client.delete(f"/api/adapters/{created['id']}")
    assert response.status_code == 204

    assert api_client.get(f"/api/adapters/{created['id']}").status_code == 404
    gone_version = api_client.get(f"/api/adapters/{created['id']}/versions/{version['id']}")
    assert gone_version.status_code == 404


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
    assert adapter["published_version_id"] is None


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


# --- Publish ----------------------------------------------------------------


def test_publish_sets_published_without_touching_latest(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    first = save_version(api_client, created["id"], code="v1")
    second = save_version(api_client, created["id"], code="v2")
    pass_publish_gate(api_client, created["id"], first["id"])

    response = api_client.post(f"/api/adapters/{created['id']}/versions/{first['id']}/publish")
    assert response.status_code == 200
    body = response.json()
    # Publishing a historical version is allowed; latest stays untouched.
    assert body["published_version_id"] == first["id"]
    assert body["latest_version_id"] == second["id"]


def test_publish_latest_version(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])
    pass_publish_gate(api_client, created["id"], version["id"])

    response = api_client.post(f"/api/adapters/{created['id']}/versions/{version['id']}/publish")
    assert response.status_code == 200
    body = response.json()
    assert body["published_version_id"] == version["id"]
    assert body["latest_version_id"] == version["id"]


def test_publish_same_version_is_idempotent(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"])
    pass_publish_gate(api_client, created["id"], version["id"])
    path = f"/api/adapters/{created['id']}/versions/{version['id']}/publish"

    assert api_client.post(path).status_code == 200
    response = api_client.post(path)
    assert response.status_code == 200
    assert response.json()["published_version_id"] == version["id"]


def test_publish_unknown_version_not_found(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    response = api_client.post(f"/api/adapters/{created['id']}/versions/99999/publish")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "version_not_found"


def test_publish_version_of_other_adapter_not_found(api_client: TestClient) -> None:
    first = create_adapter(api_client, name="first")
    second = create_adapter(api_client, name="second")
    version = save_version(api_client, first["id"])

    response = api_client.post(f"/api/adapters/{second['id']}/versions/{version['id']}/publish")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "version_not_found"


def test_publish_does_not_modify_version_content(api_client: TestClient) -> None:
    created = create_adapter(api_client)
    version = save_version(api_client, created["id"], code="original")

    api_client.post(f"/api/adapters/{created['id']}/versions/{version['id']}/publish")

    detail = api_client.get(f"/api/adapters/{created['id']}/versions/{version['id']}").json()
    assert detail["code"] == "original"
    assert detail["seq"] == 1


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
