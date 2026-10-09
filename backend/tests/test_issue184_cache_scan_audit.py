from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path

import pytest

from dlr.worker.cache_policy import CachePolicy
from test_issue161_cache_policy import _manager, _ready


def _events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(r.message.removeprefix("cache_scan_audit "))
        for r in caplog.records
        if r.message.startswith("cache_scan_audit ")
    ]


def test_periodic_audit_links_checked_key_to_reference_and_release(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    policy = replace(CachePolicy(), gc_enabled=True, idle_ttl_seconds=0, min_idle_seconds=0)
    cache, lifecycle, manager = _manager(tmp_path, policy)
    identity, digest = _ready(cache, lifecycle)
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="PRIVATE-PROOF-TEXT",
        actor="PRIVATE-ACTOR",
        valid_until=time.time() + 300,
    )
    caplog.set_level(logging.INFO)
    use = lifecycle.begin_use("11-13", worker_id=7, execution_id=23, attempt_id=29, fencing_token=1)
    with use:
        result = manager.run_round(mode="periodic")
        assert result.deleted == 0
        scans = [e for e in _events(caplog) if e["phase"] == "scan"]
        assert scans[-1]["mode"] == "periodic"
        assert scans[-1]["items"] == [
            {
                "cache_key": "11-13",
                "kind": "version",
                "bytes": cache.accounting_snapshot()["committed_bytes"],
                "reasons": ["cache_entry_in_use"],
            }
        ]
        assert scans[-1]["complete"] is True
        assert scans[-1]["budget"]["scan_nodes_remaining"] >= 0
        use.release(cleanup_completed=True)
    assert cache.entry_path("11-13").exists()
    result = manager.run_round(mode="periodic")
    assert result.deleted == 1
    events = _events(caplog)
    for complete in (e for e in events if e["phase"] == "result"):
        assert any(e["round_id"] == complete["round_id"] and e["phase"] == "scan" for e in events)
    assert events[-1]["freed_bytes"] > 0
    assert "PRIVATE-PROOF-TEXT" not in caplog.text
    assert "PRIVATE-ACTOR" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_manual_scan_pagination_hashes_unknown_names_and_bounds_items(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache, lifecycle, manager = _manager(
        tmp_path, replace(CachePolicy(), max_scan_entries_per_round=1)
    )
    _ready(cache, lifecycle)
    _ready(cache, lifecycle, adapter_id=12)
    (cache.entries / "PRIVATE-ENDPOINT-NAME").mkdir()
    caplog.set_level(logging.INFO)
    manager.scan(mode="manual")
    scan = _events(caplog)[-1]
    assert scan["mode"] == "manual" and scan["phase"] == "scan"
    assert scan["complete"] is False
    assert len(scan["items"]) <= 1
    assert len(scan["cursor_after_sha256"]) == 64
    manager.scan(mode="pressure")
    manager.scan(mode="manual")
    assert "PRIVATE-ENDPOINT-NAME" not in caplog.text
    assert str(tmp_path) not in caplog.text
