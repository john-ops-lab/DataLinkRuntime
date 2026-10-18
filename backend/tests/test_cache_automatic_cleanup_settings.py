"""Only the two administrator-controlled automatic cleanup switches."""

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dlr.control.models import Worker
from dlr.control.schemas.cache_admin import CacheOperationCreate
from dlr.control.security import Principal, require_principal
from dlr.worker import cache_policy
from dlr.worker.cache import CacheError, VerifiedVersionCache
from dlr.worker.cache_policy import CachePolicy, create_version_cache, effective_cache_policy
from test_issue161_cache_admin import WORKER_HEADERS, _local_agent, _upload
from test_issue161_cache_governance import _fixture
from test_issue161_cache_policy import _manager


def _command(field, enabled):
    return {
        "operation_id": str(uuid.uuid4()),
        "claim_epoch": 1,
        "request_hash": "a" * 64,
        "kind": "protect",
        "payload": {"kind": "protect", "policy": {field: enabled}},
        "actor": "administrator",
    }


def _agent(tmp_path):
    base = CachePolicy()
    _, lifecycle, manager = _manager(tmp_path, base)
    agent = _local_agent(lifecycle, manager)
    agent._config = SimpleNamespace(runtime_root=tmp_path, cache_policy=base)
    return agent, manager, base


@pytest.mark.parametrize("field", ["gc_enabled", "pressure_gc_enabled"])
def test_cleanup_switch_applies_persists_and_replays_without_another_write(
    tmp_path, monkeypatch, field
):
    agent, manager, base = _agent(tmp_path)
    command = _command(field, True)
    assert agent._execute_cache_command(7, command)
    assert getattr(manager.policy, field) is True
    assert getattr(create_version_cache(tmp_path, policy=base).policy, field) is True
    assert agent._client.events == ["snapshot", "command"]
    assert agent._client.snapshots[-1]["summary"]["policy"][field] is True
    assert {k: v for k, v in manager.policy.public_values().items() if k != field} == {
        k: v for k, v in base.public_values().items() if k != field
    }

    def unexpected_write(*_args, **_kwargs):
        raise AssertionError("a completed command must not apply again")

    monkeypatch.setattr("dlr.worker.agent.save_automatic_cleanup", unexpected_write)
    assert agent._execute_cache_command(7, command)
    assert agent._client.reports[-1]["status"] == "completed"


def test_cleanup_switches_are_independent_and_disabled_modes_stay_disabled(tmp_path):
    agent, manager, base = _agent(tmp_path)
    for field, enabled in [
        ("gc_enabled", True),
        ("pressure_gc_enabled", True),
        ("gc_enabled", False),
    ]:
        assert agent._execute_cache_command(7, _command(field, enabled))
    restored = effective_cache_policy(tmp_path, base)
    assert restored.gc_enabled is False and restored.pressure_gc_enabled is True
    assert manager.run_round(mode="periodic").status == "disabled"
    assert agent._execute_cache_command(7, _command("pressure_gc_enabled", False))
    assert manager.run_round(mode="pressure").status == "disabled"
    assert effective_cache_policy(tmp_path, base) == base


def test_cleanup_switch_write_failure_keeps_the_actual_old_state(tmp_path, monkeypatch):
    agent, manager, base = _agent(tmp_path)

    def fail(*_args, **_kwargs):
        raise CacheError("cache_lifecycle_write_failed")

    monkeypatch.setattr(cache_policy, "_write_json", fail)
    assert agent._execute_cache_command(7, _command("gc_enabled", True))
    assert manager.policy == base
    assert agent._client.reports[-1]["status"] == "failed"
    assert agent._client.reports[-1]["error_code"] == "cache_policy_write_failed"
    assert agent._client.snapshots[-1]["summary"]["policy"]["gc_enabled"] is False


def test_cleanup_switch_uncertain_write_reports_the_actual_persisted_value(tmp_path, monkeypatch):
    agent, manager, base = _agent(tmp_path)
    writer = cache_policy._write_json

    def uncertain(*args, **kwargs):
        writer(*args, **kwargs)
        raise CacheError("cache_lifecycle_write_failed")

    monkeypatch.setattr(cache_policy, "_write_json", uncertain)
    assert agent._execute_cache_command(7, _command("gc_enabled", True))
    assert agent._client.reports[-1]["status"] == "failed"
    assert effective_cache_policy(tmp_path, base).gc_enabled is True
    assert manager.policy.gc_enabled is True
    assert agent._client.snapshots[-1]["summary"]["policy"]["gc_enabled"] is True


@pytest.mark.parametrize(
    "mode,field", [("periodic", "gc_enabled"), ("pressure", "pressure_gc_enabled")]
)
def test_cleanup_switch_off_is_rechecked_after_acquiring_the_round_lock(
    tmp_path, monkeypatch, mode, field
):
    _, manager, _ = _agent(tmp_path)
    manager.policy = replace(manager.policy, **{field: True})
    entry_lock = manager.lifecycle.entry_lock

    class ClosedBeforeRound:
        def __enter__(self):
            self.lock = entry_lock("governance-round", blocking=False)
            self.lock.__enter__()
            manager.policy = replace(manager.policy, **{field: False})
            return self

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    monkeypatch.setattr(
        manager.lifecycle, "entry_lock", lambda *_args, **_kwargs: ClosedBeforeRound()
    )

    def must_not_scan(**_kwargs):
        raise AssertionError("a disabled mode must not start another automatic round")

    monkeypatch.setattr(manager, "scan", must_not_scan)
    assert manager.run_round(mode=mode).status == "disabled"


def test_pressure_switch_reaches_existing_cache_objects_without_restarting(tmp_path, monkeypatch):
    agent, manager, base = _agent(tmp_path)
    existing_cache = create_version_cache(tmp_path, policy=base)
    requests = []
    calls = []

    def full(*_args, **_kwargs):
        calls.append(1)
        raise CacheError("cache_low_watermark")

    monkeypatch.setattr(VerifiedVersionCache, "reserve", full)
    cache_policy.register_pressure_handler(
        existing_cache.root, lambda amount: requests.append(amount) is None
    )
    try:
        assert agent._execute_cache_command(7, _command("pressure_gc_enabled", True))
        with pytest.raises(CacheError) as insufficient:
            existing_cache.reserve(123)
        assert insufficient.value.code == "cache_capacity_insufficient"
        assert requests == [123] and len(calls) == 2
        assert agent._execute_cache_command(7, _command("pressure_gc_enabled", False))
        with pytest.raises(CacheError) as disabled:
            existing_cache.reserve(123)
        assert disabled.value.code == "cache_low_watermark"
        assert requests == [123] and len(calls) == 3
        assert manager.policy == base
    finally:
        cache_policy.register_pressure_handler(existing_cache.root, None)


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"gc_enabled": "true"},
        {"gc_enabled": 1},
        {"gc_enabled": None},
        {"offline_protection": False},
        {"min_idle_seconds": 0},
    ],
)
def test_cleanup_switch_request_accepts_only_boolean_switch_fields(policy):
    with pytest.raises(ValidationError):
        CacheOperationCreate.model_validate(
            {"kind": "protect", "idempotency_key": "switch", "policy": policy}
        )


def test_cleanup_switch_api_permissions_capability_and_idempotency(
    api_client, session_factory, monkeypatch
):
    worker, adapter, version = _fixture(api_client, session_factory, monkeypatch, "switch")
    worker_id = int(worker["id"])
    _upload(api_client, worker_id, int(adapter["id"]), version)
    url = f"/api/workers/{worker_id}/cache/operations"
    request = {"kind": "protect", "idempotency_key": "switch", "policy": {"gc_enabled": True}}
    assert TestClient(api_client.app).post(url, json=request).status_code == 401
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=123
    )
    try:
        assert api_client.post(url, json=request).status_code == 403
    finally:
        del api_client.app.dependency_overrides[require_principal]
    assert api_client.post(url, json=request).json()["detail"]["code"] == "cache_policy_unsupported"
    with session_factory.begin() as session:
        row = session.get(Worker, worker_id)
        row.isolation_capabilities = {**row.isolation_capabilities, "cache_policy_control_v1": True}
    created = api_client.post(url, json=request)
    assert created.status_code == 202, created.text
    assert created.json()["request"]["policy"] == {"gc_enabled": True}
    assert (
        api_client.post(url, json=request).json()["operation_id"] == created.json()["operation_id"]
    )
    changed = {**request, "policy": {"gc_enabled": False}}
    assert api_client.post(url, json=changed).status_code == 409
    with session_factory.begin() as session:
        row = session.get(Worker, worker_id)
        row.isolation_capabilities = {"cache_governance_v1": True}
    # A downgraded Worker cannot accidentally claim a new policy payload.
    assert (
        api_client.post(
            f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS
        ).status_code
        == 204
    )
    with session_factory.begin() as session:
        row = session.get(Worker, worker_id)
        row.isolation_capabilities = {**row.isolation_capabilities, "cache_policy_control_v1": True}
    claim = api_client.post(
        f"/api/workers/{worker_id}/cache/commands/claim", headers=WORKER_HEADERS
    )
    assert claim.status_code == 200
    assert claim.json()["payload"]["policy"] == {"gc_enabled": True}
