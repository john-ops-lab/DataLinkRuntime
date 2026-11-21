"""Capacity publication rules for every member of a PostgreSQL topology."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

PATH = Path(__file__).resolve().parents[2] / "deploy/control-ha/postgres_capacity.py"
SPEC = importlib.util.spec_from_file_location("reference_capacity_provider", PATH)
assert SPEC is not None and SPEC.loader is not None
PROVIDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROVIDER)


@pytest.fixture
def observation_files(tmp_path):
    now = datetime.now(UTC)
    topology = {
        "cluster_id": str(uuid4()),
        "storage_domain_id": "pg-data",
        "topology_epoch": 7,
        "system_identifier": "7465828699174792761",
        "timeline": 4,
        "primary": "a",
        "promotable": ["b", "c"],
        "activation_id": str(uuid4()),
        "runtime_database": "dlr",
        "runtime_role": "dlr_runtime",
    }
    top_path = tmp_path / "topology.json"
    top_path.write_text(json.dumps(topology))
    samples = [
        {
            "node": name,
            "observed_at": (now - timedelta(seconds=seconds)).isoformat(),
            "system_identifier": topology["system_identifier"],
            "timeline": 4,
            "standby": name != "a",
            "synchronous_commit": "remote_apply",
            "synchronous_peers": ["c", "b"] if name == "a" else [],
            "synchronous_standby_names": "FIRST 2 (b, c)" if name == "a" else "",
            "runtime_settings_safe": True,
            "persistent_wal": True,
            "data_free_bytes": 13_000,
            "wal_free_bytes": wal_free,
            "activation_id": topology["activation_id"],
            "runtime_database": "dlr",
            "runtime_role": "dlr_runtime",
        }
        for name, wal_free, seconds in (("a", 10_000, 3), ("b", 12_000, 1), ("c", 2_500, 2))
    ]
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({"samples": samples}))
    return top_path, batch, samples


def test_capacity_uses_all_members_and_earliest_observation(observation_files):
    topology, batch, samples = observation_files
    result = PROVIDER.capacity(topology, batch)
    assert result == {
        "schema_version": 1,
        "storage_domain_id": "pg-data",
        "cluster_id": json.loads(topology.read_text())["cluster_id"],
        "topology_epoch": 7,
        "observed_at": samples[0]["observed_at"],
        "writable_free_bytes": 2_500,
    }


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (0, "synchronous_commit", "off"),
        (0, "synchronous_standby_names", "FIRST 1 (b, c)"),
        (0, "synchronous_peers", ["b"]),
        (0, "runtime_settings_safe", False),
        (2, "persistent_wal", False),
        (2, "system_identifier", "7465828699174792762"),
        (2, "standby", False),
        (2, "timeline", 3),
        (2, "wal_free_bytes", -1),
        (2, "node", "b"),
    ],
)
def test_mismatched_promotion_member_closes_capacity(observation_files, index, field, value):
    topology, batch, samples = observation_files
    samples[index][field] = value
    batch.write_text(json.dumps({"samples": samples}))
    with pytest.raises(ValueError):
        PROVIDER.capacity(topology, batch)


def test_missing_or_stale_final_member_closes_capacity(observation_files):
    topology, batch, samples = observation_files
    batch.write_text(json.dumps({"samples": samples[:2]}))
    with pytest.raises(ValueError):
        PROVIDER.capacity(topology, batch)
    samples[2]["observed_at"] = (datetime.now(UTC) - timedelta(seconds=11)).isoformat()
    batch.write_text(json.dumps({"samples": samples}))
    with pytest.raises(ValueError):
        PROVIDER.capacity(topology, batch)


def test_fresh_prior_activation_is_rejected(observation_files):
    topology, batch, samples = observation_files
    samples[2]["activation_id"] = str(uuid4())
    batch.write_text(json.dumps({"samples": samples}))
    with pytest.raises(ValueError):
        PROVIDER.capacity(topology, batch)
