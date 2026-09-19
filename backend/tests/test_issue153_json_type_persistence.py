"""Issue #153A regression tests for exact JSON type persistence."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Execution
from dlr.control.services.schedule import _json_values_equal
from runtime_api_support import claim_execution, report_attempt
from test_adapters import create_adapter, save_version

TYPE_TRANSITIONS = [
    pytest.param(0, False, "boolean", None, None, id="top-0-to-false"),
    pytest.param(False, 0, "number", None, None, id="top-false-to-0"),
    pytest.param(1, True, "boolean", None, None, id="top-1-to-true"),
    pytest.param(True, 1, "number", None, None, id="top-true-to-1"),
    pytest.param(
        {"outer": {"flag": 0}},
        {"outer": {"flag": False}},
        "object",
        "boolean",
        None,
        id="nested-object-0-to-false",
    ),
    pytest.param(
        {"items": [0, 1]},
        {"items": [False, True]},
        "object",
        None,
        ["boolean", "boolean"],
        id="nested-list-numbers-to-booleans",
    ),
]


def _exact_json(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _exact_json(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _exact_json(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    return bool(actual == expected)


def _create_adapter(client: TestClient, name: str) -> int:
    response = client.post(
        "/api/adapters",
        json={
            "name": name,
            "description": "issue153 exact JSON persistence",
            "language": "python",
            "adapter_type": "task",
        },
    )
    assert response.status_code == 201, response.text
    adapter_id = int(response.json()["id"])
    mode = client.patch(f"/api/adapters/{adapter_id}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    return adapter_id


def _put_schedule(client: TestClient, adapter_id: int, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "enabled": False,
        "cron": "0 * * * *",
        "timezone": "UTC",
    }
    payload.update(overrides)
    response = client.put(f"/api/adapters/{adapter_id}/schedule", json=payload)
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _put_input(
    client: TestClient,
    adapter_id: int,
    *,
    revision: int,
    source_type: str,
    json_value: Any = ...,
) -> httpx.Response:
    payload: dict[str, Any] = {
        "expected_revision": revision,
        "source_type": source_type,
    }
    if json_value is not ...:
        payload["json_value"] = json_value
    return cast(
        httpx.Response,
        client.put(f"/api/adapters/{adapter_id}/input-config", json=payload),
    )


def _json_row(session_factory: sessionmaker[Session], adapter_id: int) -> dict[str, Any]:
    with session_factory() as session:
        row = (
            session.execute(
                text(
                    """
                SELECT c.source_type,
                       c.revision,
                       c.json_value,
                       c.json_value IS NULL AS config_is_sql_null,
                       jsonb_typeof(c.json_value) AS config_top_type,
                       jsonb_typeof(c.json_value -> 'outer' -> 'flag') AS config_object_type,
                       jsonb_typeof(c.json_value -> 'items' -> 0) AS config_item_0_type,
                       jsonb_typeof(c.json_value -> 'items' -> 1) AS config_item_1_type,
                       jsonb_typeof(c.json_value -> 'nested' -> 'b') AS config_nested_b_type,
                       s.input AS schedule_input,
                       jsonb_typeof(s.input) AS schedule_top_type,
                       jsonb_typeof(s.input -> 'outer' -> 'flag') AS schedule_object_type,
                       jsonb_typeof(s.input -> 'items' -> 0) AS schedule_item_0_type,
                       jsonb_typeof(s.input -> 'items' -> 1) AS schedule_item_1_type,
                       jsonb_typeof(s.input -> 'nested' -> 'b') AS schedule_nested_b_type,
                       s.enabled AS schedule_enabled
                  FROM adapter_input_configs AS c
                  JOIN adapter_schedules AS s ON s.adapter_id = c.adapter_id
                 WHERE c.adapter_id = :adapter_id
                """
                ),
                {"adapter_id": adapter_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


@pytest.mark.parametrize(
    ("initial", "target", "top_type", "object_type", "item_types"),
    TYPE_TRANSITIONS,
)
def test_input_api_persists_exact_json_type_and_schedule_mirror(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    initial: Any,
    target: Any,
    top_type: str,
    object_type: str | None,
    item_types: list[str] | None,
) -> None:
    adapter_id = _create_adapter(api_client, f"issue153-transition-{top_type}")
    _put_schedule(api_client, adapter_id, input=initial)

    response = _put_input(
        api_client,
        adapter_id,
        revision=2,
        source_type="json",
        json_value=target,
    )

    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 3
    assert _exact_json(response.json()["json_value"], target)
    reloaded = api_client.get(f"/api/adapters/{adapter_id}/input-config").json()
    assert _exact_json(reloaded["json_value"], target)
    schedule = api_client.get(f"/api/adapters/{adapter_id}/schedule").json()
    assert _exact_json(schedule["input"], target)

    row = _json_row(session_factory, adapter_id)
    assert row["revision"] == 3
    assert _exact_json(row["json_value"], target)
    assert _exact_json(row["schedule_input"], target)
    assert row["config_top_type"] == top_type
    assert row["schedule_top_type"] == top_type
    assert row["config_object_type"] == object_type
    assert row["schedule_object_type"] == object_type
    expected_items: list[str | None] = list(item_types) if item_types is not None else [None, None]
    assert [row["config_item_0_type"], row["config_item_1_type"]] == expected_items
    assert [row["schedule_item_0_type"], row["schedule_item_1_type"]] == expected_items


def test_json_null_and_sql_null_keep_distinct_storage_contracts(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = _create_adapter(api_client, "issue153-null-contract")
    _put_schedule(api_client, adapter_id, input={"before": True})

    json_null = _put_input(
        api_client,
        adapter_id,
        revision=2,
        source_type="json",
        json_value=None,
    )
    assert json_null.status_code == 200, json_null.text
    assert json_null.json()["revision"] == 3
    row = _json_row(session_factory, adapter_id)
    assert row["config_is_sql_null"] is False
    assert row["config_top_type"] == "null"
    assert row["schedule_top_type"] == "null"

    no_input = _put_input(
        api_client,
        adapter_id,
        revision=3,
        source_type="none",
    )
    assert no_input.status_code == 200, no_input.text
    assert no_input.json()["revision"] == 4
    row = _json_row(session_factory, adapter_id)
    assert row["source_type"] == "none"
    assert row["config_is_sql_null"] is True
    assert row["config_top_type"] is None
    assert row["schedule_top_type"] == "null"


def test_ordinary_and_unchanged_json_saves_still_advance_revision(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = _create_adapter(api_client, "issue153-ordinary-values")
    _put_schedule(api_client, adapter_id, input={"seed": True})

    for revision, value, postgres_type in [
        (2, 7, "number"),
        (3, "plain", "string"),
        (4, "plain", "string"),
    ]:
        response = _put_input(
            api_client,
            adapter_id,
            revision=revision,
            source_type="json",
            json_value=value,
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == revision + 1
        row = _json_row(session_factory, adapter_id)
        assert row["revision"] == revision + 1
        assert _exact_json(row["json_value"], value)
        assert _exact_json(row["schedule_input"], value)
        assert row["config_top_type"] == postgres_type
        assert row["schedule_top_type"] == postgres_type


@pytest.mark.parametrize(
    ("initial", "target", "top_type", "object_type", "item_types"),
    TYPE_TRANSITIONS,
)
def test_legacy_schedule_write_preserves_numeric_boolean_type_transition(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    initial: Any,
    target: Any,
    top_type: str,
    object_type: str | None,
    item_types: list[str] | None,
) -> None:
    adapter_id = _create_adapter(api_client, "issue153-legacy-schedule")
    _put_schedule(api_client, adapter_id, input=initial)

    response = _put_schedule(api_client, adapter_id, input=target)

    assert _exact_json(response["input"], target)
    config = api_client.get(f"/api/adapters/{adapter_id}/input-config").json()
    assert config["revision"] == 3
    assert _exact_json(config["json_value"], target)
    row = _json_row(session_factory, adapter_id)
    assert row["revision"] == 3
    assert _exact_json(row["json_value"], target)
    assert _exact_json(row["schedule_input"], target)
    assert row["config_top_type"] == top_type
    assert row["schedule_top_type"] == top_type
    assert row["config_object_type"] == object_type
    assert row["schedule_object_type"] == object_type
    expected_items: list[str | None] = list(item_types) if item_types is not None else [None, None]
    assert [row["config_item_0_type"], row["config_item_1_type"]] == expected_items
    assert [row["schedule_item_0_type"], row["schedule_item_1_type"]] == expected_items


def test_enabled_schedule_disable_only_rejects_type_change_but_accepts_equal_reordering(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = _create_adapter(api_client, "issue153-locked-disable")
    save_version(api_client, adapter_id)
    initial = {"nested": {"a": 1, "b": False}}
    _put_schedule(api_client, adapter_id, enabled=True, input=initial)
    locked_baseline = _json_row(session_factory, adapter_id)

    changed = api_client.put(
        f"/api/adapters/{adapter_id}/schedule",
        json={
            "enabled": False,
            "cron": "0 * * * *",
            "timezone": "UTC",
            "input": {"nested": {"a": 1, "b": 0}},
        },
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "adapter_runtime_locked"
    locked_after_rejection = _json_row(session_factory, adapter_id)
    assert locked_after_rejection == locked_baseline
    assert _exact_json(locked_after_rejection["json_value"], initial)
    assert _exact_json(locked_after_rejection["schedule_input"], initial)
    assert locked_after_rejection["config_nested_b_type"] == "boolean"
    assert locked_after_rejection["schedule_nested_b_type"] == "boolean"

    equal_reordered = api_client.put(
        f"/api/adapters/{adapter_id}/schedule",
        json={
            "enabled": False,
            "cron": "0 * * * *",
            "timezone": "UTC",
            "input": {"nested": {"b": False, "a": 1.0}},
        },
    )
    assert equal_reordered.status_code == 200, equal_reordered.text
    assert equal_reordered.json()["enabled"] is False
    assert _exact_json(equal_reordered.json()["input"], initial)
    config = api_client.get(f"/api/adapters/{adapter_id}/input-config").json()
    assert config["revision"] == 2
    assert _exact_json(config["json_value"], initial)
    row = _json_row(session_factory, adapter_id)
    assert row["revision"] == 2
    assert row["schedule_enabled"] is False
    assert _exact_json(row["json_value"], initial)
    assert _exact_json(row["schedule_input"], initial)
    assert row["config_nested_b_type"] == "boolean"
    assert row["schedule_nested_b_type"] == "boolean"


def _nested_object(depth: int, leaf: object) -> object:
    value = leaf
    for _ in range(depth):
        value = {"value": value}
    return value


def test_json_comparison_handles_deep_valid_input_without_recursion() -> None:
    same_left = _nested_object(500, {"flag": 1})
    same_right = json.loads(json.dumps(same_left))
    assert _json_values_equal(same_left, same_right)

    numeric = _nested_object(600, {"flag": 0})
    boolean = json.loads(json.dumps(_nested_object(600, {"flag": False})))
    assert not _json_values_equal(numeric, boolean)


def test_failed_updates_leave_value_revision_and_mirror_unchanged(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = _create_adapter(api_client, "issue153-failed-updates")
    _put_schedule(api_client, adapter_id, input={"stable": 1})
    baseline = _json_row(session_factory, adapter_id)

    stale = _put_input(
        api_client,
        adapter_id,
        revision=1,
        source_type="json",
        json_value={"changed": True},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "input_config_revision_conflict"
    assert _json_row(session_factory, adapter_id) == baseline

    invalid_source = _put_input(
        api_client,
        adapter_id,
        revision=2,
        source_type="remote_files",
    )
    assert invalid_source.status_code == 422
    assert invalid_source.json()["detail"]["code"] == "input_source_not_available"
    assert _json_row(session_factory, adapter_id) == baseline

    invalid_fields = _put_input(
        api_client,
        adapter_id,
        revision=2,
        source_type="none",
        json_value=None,
    )
    assert invalid_fields.status_code == 422
    assert _json_row(session_factory, adapter_id) == baseline

    save_version(api_client, adapter_id)
    _put_schedule(api_client, adapter_id, enabled=True)
    locked_baseline = _json_row(session_factory, adapter_id)
    locked = _put_input(
        api_client,
        adapter_id,
        revision=2,
        source_type="json",
        json_value={"changed": True},
    )
    assert locked.status_code == 409
    assert locked.json()["detail"]["code"] == "adapter_runtime_locked"
    assert _json_row(session_factory, adapter_id) == locked_baseline


def test_input_update_keeps_old_execution_immutable_and_new_execution_uses_new_value(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="issue153-execution-history")
    save_version(api_client, adapter["id"])
    saved = _put_input(
        api_client,
        adapter["id"],
        revision=1,
        source_type="json",
        json_value={"flag": 0},
    )
    assert saved.status_code == 200, saved.text

    old_response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert old_response.status_code == 202, old_response.text
    old_execution = old_response.json()
    adapter_state = api_client.get(f"/api/adapters/{adapter['id']}").json()
    worker_id = adapter_state["runtime_worker_id"]
    assert worker_id is not None
    claimed = claim_execution(
        api_client,
        worker_id,
        execution_id=old_execution["id"],
    )
    assert claimed.status_code == 200, claimed.text
    completed = report_attempt(
        api_client,
        worker_id,
        old_execution["id"],
        {"status": "succeeded", "output": {"stable": 0}},
    )
    assert completed.status_code == 200, completed.text

    updated = _put_input(
        api_client,
        adapter["id"],
        revision=2,
        source_type="json",
        json_value={"flag": False},
    )
    assert updated.status_code == 200, updated.text
    new_response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert new_response.status_code == 202, new_response.text
    new_execution = new_response.json()

    with session_factory() as session:
        old_row = session.get(Execution, old_execution["id"])
        new_row = session.get(Execution, new_execution["id"])
        assert old_row is not None and new_row is not None
        assert old_row.status == "succeeded"
        assert _exact_json(old_row.input, {"flag": 0})
        assert old_row.input_config_revision == 2
        assert old_row.input_snapshot == {"source_type": "json", "revision": 2}
        assert _exact_json(old_row.output, {"stable": 0})
        assert _exact_json(new_row.input, {"flag": False})
        assert new_row.input_config_revision == 3
        assert new_row.input_snapshot == {"source_type": "json", "revision": 3}
