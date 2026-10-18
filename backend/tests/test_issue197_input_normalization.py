"""Issue #197 JSONB input normalization and atomic write contracts."""

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Adapter
from dlr.control.schemas.input_config import AdapterInputConfigUpsert
from dlr.control.services.input_config import normalize_json_input
from test_adapters import create_adapter, save_version
from test_portable_packages import import_adapter, package, worker


def test_normalizer_preserves_json_and_is_idempotent() -> None:
    raw = {
        "a\x00b": ["A\x00\x00B", "\x00", "", "A\\u0000B", '中\n\t"\\文', 0, False, 1, True, None],
        "__proto__": {"constructor": "\x00"},
    }
    expected = {
        "ab": ["AB", "", "", "A\\u0000B", '中\n\t"\\文', 0, False, 1, True, None],
        "__proto__": {"constructor": ""},
    }
    value, count = normalize_json_input(raw)
    assert value == expected and count == 5
    assert normalize_json_input(value) == (value, 0)
    assert raw["a\x00b"][0] == "A\x00\x00B"
    assert type(value["ab"][5]) is int and type(value["ab"][6]) is bool


def test_collision_reports_json_pointer() -> None:
    with pytest.raises(HTTPException) as error:
        normalize_json_input({"a/b~c": [{"a\x00b": 1, "ab": 2}]})
    assert error.value.detail["code"] == "input_json_key_conflict"
    assert error.value.detail["params"] == {
        "path": "/a~1b~0c/0",
        "keys": ["a\x00b", "ab"],
        "key": "ab",
    }


@pytest.mark.parametrize(
    "raw,expected,count",
    [
        ("A\x00B", "AB", 1),
        ("\x00\x00", "", 2),
        ({"a\x00b": ["\x00", "中\n\t", "\\u0000"]}, {"ab": ["", "中\n\t", "\\u0000"]}, 2),
        (None, None, 0),
        (0, 0, 0),
        (False, False, 0),
        (1, 1, 0),
        (True, True, 0),
    ],
)
def test_api_saved_value_and_count(
    api_client: TestClient, raw: Any, expected: Any, count: int
) -> None:
    adapter = create_adapter(api_client, name="normalized")
    url = f"/api/adapters/{adapter['id']}/input-config"
    saved = api_client.put(
        url, json={"expected_revision": 1, "source_type": "json", "json_value": raw}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["json_value"] == expected
    assert type(saved.json()["json_value"]) is type(expected)
    assert saved.json()["removed_null_characters"] == count
    reread = api_client.get(url).json()
    assert reread["json_value"] == expected and reread["removed_null_characters"] == 0
    assert reread["revision"] == 2


def test_collision_and_size_leave_original_config(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, name="atomic")
    url = f"/api/adapters/{adapter['id']}/input-config"
    api_client.put(
        url, json={"expected_revision": 1, "source_type": "json", "json_value": {"saved": True}}
    )
    before = api_client.get(url).json()
    collision = api_client.put(
        url,
        json={"expected_revision": 2, "source_type": "json", "json_value": {"ab": 1, "a\x00b": 2}},
    )
    assert (
        collision.status_code == 422
        and collision.json()["detail"]["code"] == "input_json_key_conflict"
    )
    assert api_client.get(url).json() == before
    monkeypatch.setattr(settings, "execution_input_max_bytes", 64)
    for value, size in [("\x00" * 64, None), ("ok", 100), ("x" * 100, 0)]:
        body = {"expected_revision": 2, "source_type": "json", "json_value": value}
        if size is not None:
            body["original_json_size_bytes"] = size
        response = api_client.put(url, json=body)
        assert response.status_code == 413, response.text
        assert api_client.get(url).json() == before


def test_schedule_mirror_and_execution_snapshot(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="schedule-normalized")
    save_version(api_client, adapter["id"])
    api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    url = f"/api/adapters/{adapter['id']}/schedule"
    saved = api_client.put(
        url,
        json={
            "enabled": False,
            "cron": "0 * * * *",
            "timezone": "UTC",
            "input": {"a\x00b": "\x00"},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["input"] == {"ab": ""}
    current = api_client.get(f"/api/adapters/{adapter['id']}/input-config").json()
    assert current["json_value"] == saved.json()["input"]
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    assert execution.json()["input"] == {"ab": ""}


def test_import_normalizes_and_collision_rolls_back(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    value = package()
    value["schedule"] = {"cron": "0 * * * *", "timezone": "UTC"}
    value["input"] = {"source_type": "json", "included": True, "json_value": {"a\x00b": "A\x00B"}}
    worker_id = worker(session_factory, "python")
    saved = import_adapter(api_client, value, worker_id)
    assert saved.status_code == 201, saved.text
    adapter_id = saved.json()["id"]
    assert api_client.get(f"/api/adapters/{adapter_id}/input-config").json()["json_value"] == {
        "ab": "AB"
    }
    assert api_client.get(f"/api/adapters/{adapter_id}/schedule").json()["input"] == {"ab": "AB"}
    with session_factory() as session:
        before = session.scalar(select(func.count()).select_from(Adapter))
    value["input"]["json_value"] = {"ab": 1, "a\x00b": 2}
    failed = import_adapter(api_client, value, worker_id)
    assert failed.status_code == 422, failed.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Adapter)) == before


def test_json_null_and_sql_null_remain_distinct(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="null-type")
    url = f"/api/adapters/{adapter['id']}/input-config"
    for revision, source, value in [(1, "json", None), (2, "none", None)]:
        body = {"expected_revision": revision, "source_type": source}
        if source == "json":
            body["json_value"] = value
        response = api_client.put(url, json=body)
        assert response.status_code == 200, response.text
        with session_factory() as session:
            is_sql_null = session.scalar(
                text("SELECT json_value IS NULL FROM adapter_input_configs WHERE adapter_id = :id"),
                {"id": adapter["id"]},
            )
            assert is_sql_null is (source == "none")


@pytest.mark.parametrize("size", [-1, True, 1.5, None, "100"])
def test_original_size_requires_nonnegative_integer(size: Any) -> None:
    with pytest.raises(ValidationError):
        AdapterInputConfigUpsert(
            expected_revision=1, source_type="json", json_value=None, original_json_size_bytes=size
        )


@pytest.mark.parametrize("source", ["none", "managed_files", "remote_files"])
def test_original_size_is_json_only(source: Any) -> None:
    with pytest.raises(ValidationError):
        AdapterInputConfigUpsert(
            expected_revision=1, source_type=source, original_json_size_bytes=1
        )
