"""Storage observation follows default PGDATA and never an implicit fallback."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from dlr.common.config import Settings, settings
from dlr.control.services import ingress_capacity as capacity

ROOT = Path(__file__).resolve().parents[2]
ANCHOR = "/var/lib/dlr/ingress-storage"


def test_host_fixture_supplies_only_missing_storage_configuration(test_ingress_storage_root):
    if "DLR_INGRESS_STORAGE_ROOT" in os.environ:
        assert settings.ingress_storage_root == os.environ["DLR_INGRESS_STORAGE_ROOT"]
    else:
        assert settings.ingress_storage_root == str(test_ingress_storage_root)
        assert test_ingress_storage_root.is_dir()


def test_default_compose_observes_same_postgres_volume_readonly_without_copying():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    pgdata = [
        row
        for row in compose["services"]["postgres"]["volumes"]
        if isinstance(row, str) and row.endswith(":/var/lib/postgresql/data")
    ]
    assert len(pgdata) == 1
    source = pgdata[0].split(":", 1)[0]
    observed = [
        row
        for row in compose["services"]["control"]["volumes"]
        if isinstance(row, dict) and row.get("target") == ANCHOR
    ]
    assert len(observed) == 1
    assert observed[0] == {
        "type": "volume",
        "source": source,
        "target": ANCHOR,
        "read_only": True,
        "volume": {"nocopy": True},
    }
    assert source == "dlr_pg_data" and source in compose["volumes"]
    assert compose["services"]["control"]["environment"]["DLR_INGRESS_STORAGE_ROOT"] == (
        "${DLR_INGRESS_STORAGE_ROOT:-" + ANCHOR + "}"
    )
    example = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert example["DLR_INGRESS_STORAGE_ROOT"] == ANCHOR
    assert Settings.model_fields["ingress_storage_root"].default == ANCHOR


@pytest.mark.parametrize("kind", ["empty", "cwd", "relative", "missing", "file", "nul"])
def test_invalid_anchor_never_checks_another_filesystem_or_creates_path(
    tmp_path, monkeypatch, kind
):
    file = tmp_path / "file"
    file.write_text("preserve")
    value = {
        "empty": "",
        "cwd": ".",
        "relative": "relative/storage",
        "missing": str(tmp_path / "not-created"),
        "file": str(file),
        "nul": str(tmp_path) + "/\0",
    }[kind]
    monkeypatch.setattr(settings, "ingress_storage_root", value)

    def no_fallback(path):
        pytest.fail("Invalid storage root must not reach disk_usage")

    monkeypatch.setattr(capacity.shutil, "disk_usage", no_fallback)
    with pytest.raises(HTTPException) as raised:
        capacity.require_disk_capacity()
    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "code": "ingress_disk_unavailable",
        "message": "Persistent storage is unavailable",
    }
    assert not capacity._storage_budget_available(capacity.parse_profile(None))
    assert not (tmp_path / "not-created").exists()
    assert file.read_text() == "preserve"


def test_existing_readonly_directory_is_observed_without_writes(tmp_path, monkeypatch):
    storage = tmp_path / "pgdata"
    storage.mkdir()
    sentinel = storage / "data"
    sentinel.write_bytes(b"database bytes remain unchanged")
    storage.chmod(0o555)
    seen = []

    def usage(path):
        seen.append(path)
        return SimpleNamespace(free=6 * 1024**3)

    monkeypatch.setattr(settings, "ingress_storage_root", str(storage))
    monkeypatch.setattr(capacity.shutil, "disk_usage", usage)
    monkeypatch.setattr(capacity, "_disk_guard", capacity.DiskCapacityGuard())
    try:
        capacity.require_disk_capacity()
        assert capacity._storage_budget_available(capacity.parse_profile(None))
        assert seen == [storage, storage]
        assert sentinel.read_bytes() == b"database bytes remain unchanged"
        assert set(storage.iterdir()) == {sentinel}
        assert storage.stat().st_mode & 0o777 == 0o555
    finally:
        storage.chmod(0o755)


def test_unreadable_capacity_anchor_closes_both_guard_and_adoption(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ingress_storage_root", str(tmp_path))

    def unavailable(path):
        assert path == tmp_path
        raise PermissionError("unavailable")

    monkeypatch.setattr(capacity.shutil, "disk_usage", unavailable)
    with pytest.raises(HTTPException) as raised:
        capacity.require_disk_capacity()
    assert raised.value.detail["code"] == "ingress_disk_unavailable"
    assert not capacity._storage_budget_available(capacity.parse_profile(None))


def test_same_configured_domain_retains_five_six_gib_hysteresis(tmp_path, monkeypatch):
    gib = 1024**3
    free = [6 * gib]
    monkeypatch.setattr(settings, "ingress_storage_root", str(tmp_path))
    monkeypatch.setattr(capacity, "_disk_guard", capacity.DiskCapacityGuard())
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: SimpleNamespace(free=free[0]))
    capacity.require_disk_capacity()
    for amount in [5 * gib, 6 * gib - 1]:
        free[0] = amount
        with pytest.raises(HTTPException) as raised:
            capacity.require_disk_capacity()
        assert raised.value.detail["code"] == "ingress_disk_low"
    free[0] = 6 * gib
    capacity.require_disk_capacity()
