"""Deployment capacity validation and disk hysteresis, without external services."""

import json
from typing import Any

import pytest
from fastapi import HTTPException

from dlr.common.config import Settings, settings
from dlr.control.services import ingress_capacity as capacity


def formal_profile() -> str:
    return json.dumps(
        {
            "global": {
                "pending_count": 24000,
                "pending_bytes": 512 * 1024**2,
                "history_count": 60000,
                "history_bytes": 512 * 1024**2,
                "dedupe_count": 200000,
                "rate": 500,
            },
            "subscription_max": {
                "pending_count": 12000,
                "pending_bytes": 256 * 1024**2,
                "history_count": 30000,
                "history_bytes": 256 * 1024**2,
                "dedupe_count": 100000,
                "rate": 250,
            },
        }
    )


def test_default_profile_preserves_existing_limits() -> None:
    profile = capacity.parse_profile(None)
    assert dict(profile.global_limits) == capacity.DEFAULT_GLOBAL_LIMITS
    assert dict(profile.subscription_max) == capacity.DEFAULT_SUBSCRIPTION_LIMITS
    assert profile == capacity.parse_profile("")
    assert profile == capacity.parse_profile("{}")


def test_fingerprint_is_semantic_and_cached_profile_cannot_be_mutated() -> None:
    compact = formal_profile()
    pretty = json.dumps(json.loads(compact), indent=2, sort_keys=True)
    profile = capacity.parse_profile(compact)
    assert profile.fingerprint == capacity.parse_profile(pretty).fingerprint
    assert len(profile.fingerprint) == 64
    detached = profile.as_dict()
    detached["global"]["rate"] = 1
    assert profile.global_limits["rate"] == 500
    with pytest.raises(TypeError):
        profile.global_limits["rate"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        "null",
        "false",
        '{"globals":{}}',
        '{"global":{"pending_count":true}}',
        '{"global":{"rate":0}}',
        '{"global":{"rate":1.5}}',
        '{"global":{"rate":NaN}}',
        '{"global":{"rate":9223372036854775808}}',
        '{"global":{"rate":50,"rate":10}}',
        '{"global":{},"global":{}}',
        '{"global":{"pending_count":100001}}',
        '{"global":{"pending_bytes":1073741825}}',
        '{"subscription_max":{"history_count":500001}}',
        '{"subscription_max":{"rate":51}}',
        '{"global":{"history_bytes":1}}',
        '{"global":{"pending_bytes":1}}',
        "{" + " " * 16384 + "}",
    ],
)
def test_invalid_profiles_fail_closed_without_echoing_input(raw: str) -> None:
    with pytest.raises(capacity.CapacityProfileError) as error:
        capacity.parse_profile(raw)
    assert raw not in str(error.value)


def test_entry_can_select_formal_limit_but_default_is_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    assert capacity.subscription_limits({})["rate"] == 10
    assert capacity.subscription_limits({"limits": {"rate": 250}})["rate"] == 250
    with pytest.raises(HTTPException) as error:
        capacity.subscription_limits({"limits": {"rate": 251}})
    assert error.value.detail["code"] == "subscription_config_invalid"


@pytest.mark.parametrize("limits", [{"rate": True}, [], {"unknown": 2}, {"pending_count": 10001}])
def test_invalid_entry_limits_are_rejected(monkeypatch: pytest.MonkeyPatch, limits: Any) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    with pytest.raises(HTTPException) as error:
        capacity.subscription_limits({"limits": limits})
    assert error.value.status_code == 422


def test_lower_profile_clamps_only_missing_entry_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "ingress_capacity_profile",
        '{"global":{"rate":5},"subscription_max":{"rate":2}}',
    )
    assert capacity.subscription_limits({})["rate"] == 2
    with pytest.raises(HTTPException):
        capacity.subscription_limits({"limits": {"rate": 10}})


def test_invalid_deployment_keeps_settings_readable_but_rejects_new_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(DLR_INGRESS_CAPACITY_PROFILE="invalid")
    assert configured.ingress_capacity_profile == "invalid"
    monkeypatch.setattr(settings, "ingress_capacity_profile", "invalid")
    for get_limits in (capacity.global_limits, lambda: capacity.subscription_limits({})):
        with pytest.raises(HTTPException) as error:
            get_limits()
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "ingress_capacity_unavailable"


def test_disk_low_water_retains_block_and_restart_cannot_skip_recovery() -> None:
    guard = capacity.DiskCapacityGuard()
    gib = 1024**3
    with pytest.raises(HTTPException):
        guard.check(5 * gib + 1)
    guard.check(6 * gib)
    guard.check(5 * gib + 1)
    with pytest.raises(HTTPException) as error:
        guard.check(5 * gib)
    assert error.value.detail["code"] == "ingress_disk_low"
    with pytest.raises(HTTPException):
        guard.check(6 * gib - 1)
    guard.check(6 * gib)


def test_unavailable_disk_is_not_reported_as_message_damage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_: Any) -> Any:
        raise OSError("unavailable")

    monkeypatch.setattr(capacity.shutil, "disk_usage", unavailable)
    with pytest.raises(HTTPException) as error:
        capacity.require_disk_capacity()
    assert error.value.detail["code"] == "ingress_disk_unavailable"


def test_http_capability_defaults_closed_and_can_be_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DLR_HTTP_INGRESS_ENABLED", raising=False)
    assert Settings().http_ingress_enabled is False
    assert Settings(DLR_HTTP_INGRESS_ENABLED=True).http_ingress_enabled is True


def test_existing_individually_tightened_entry_limits_remain_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    assert capacity.subscription_limits({"limits": {"history_count": 1}})["history_count"] == 1


def test_storage_preflight_preserves_margin_without_double_counting_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    profile = capacity.parse_profile(None)
    gib = 1024**3
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: SimpleNamespace(free=6 * gib))
    assert capacity._storage_budget_available(profile)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: SimpleNamespace(free=6 * gib - 1))
    assert not capacity._storage_budget_available(profile)
