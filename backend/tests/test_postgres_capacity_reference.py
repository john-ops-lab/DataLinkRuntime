"""Capacity provider publication rules for a named synchronous PostgreSQL topology."""

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
        "primary": "b",
        "promotable": ["c"],
        "activated_at": (now - timedelta(seconds=5)).isoformat(),
    }
    observations = tmp_path / "observations"
    observations.mkdir()
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(topology))
    for name, wal_free in (("b", 12_000), ("c", 2_500)):
        (observations / f"{name}.json").write_text(
            json.dumps(
                {
                    "node": name,
                    "observed_at": (now - timedelta(seconds=1 if name == "b" else 2)).isoformat(),
                    "system_identifier": topology["system_identifier"],
                    "timeline": 4,
                    "standby": name == "c",
                    "synchronous_commit": "remote_apply",
                    "synchronous_peers": ["c"] if name == "b" else [],
                    "synchronous_standby_names": "FIRST 1 (c)" if name == "b" else "",
                    "persistent_wal": True,
                    "data_free_bytes": 13_000,
                    "wal_free_bytes": wal_free,
                }
            )
        )
    return path, observations


def test_capacity_uses_limiting_actual_volume_and_earliest_observation(observation_files):
    topology, observations = observation_files
    result = PROVIDER.capacity(topology, observations)
    standby = json.loads((observations / "c.json").read_text())
    assert result == {
        "schema_version": 1,
        "storage_domain_id": "pg-data",
        "cluster_id": json.loads(topology.read_text())["cluster_id"],
        "topology_epoch": 7,
        "observed_at": standby["observed_at"],
        "writable_free_bytes": 2_500,
    }


@pytest.mark.parametrize(
    ("node", "field", "value"),
    [
        ("b", "synchronous_commit", "off"),
        ("b", "synchronous_standby_names", "FIRST 1 (a)"),
        ("b", "synchronous_peers", []),
        ("c", "persistent_wal", False),
        ("c", "system_identifier", "7465828699174792762"),
        ("c", "standby", False),
        ("c", "timeline", 3),
        ("c", "wal_free_bytes", -1),
    ],
)
def test_mismatched_promotion_member_cannot_authorize_capacity(
    observation_files, node, field, value
):
    topology, observations = observation_files
    path = observations / f"{node}.json"
    mismatched = json.loads(path.read_text())
    mismatched[field] = value
    path.write_text(json.dumps(mismatched))
    with pytest.raises(ValueError):
        PROVIDER.capacity(topology, observations)


def test_prior_topology_observation_rejected_while_it_is_still_fresh(observation_files):
    topology, observations = observation_files
    path = observations / "c.json"
    stale_epoch = json.loads(path.read_text())
    stale_epoch["observed_at"] = (datetime.now(UTC) - timedelta(seconds=7)).isoformat()
    path.write_text(json.dumps(stale_epoch))
    with pytest.raises(ValueError):
        PROVIDER.capacity(topology, observations)
