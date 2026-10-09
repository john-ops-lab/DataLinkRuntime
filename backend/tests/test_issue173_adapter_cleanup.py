from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dlr.worker.cache import CacheError
from dlr.worker.cache_deletion import CacheDeletionManager, DeletionEligibility
from dlr.worker.cache_policy import CachePolicy
from test_issue161_cache_policy import _manager, _ready, _UnusedClient


class CleanupClient(_UnusedClient):
    def acquire_cache_guard(self, worker_id: int, **kwargs: Any) -> dict[str, Any]:
        result = super().acquire_cache_guard(worker_id, **kwargs)
        context = kwargs.get("cleanup_context") or {}
        result.update(
            cleanup_id=context.get("cleanup_id"),
            cleanup_claim_attempt=context.get("claim_attempt"),
            observed_identity=kwargs.get("observed_identity"),
            operation_kind="cleanup",
        )
        self.operations[kwargs["operation_id"]] = result
        return dict(result)

    def list_cache_guard_page(
        self,
        worker_id: int,
        *,
        after_version_id: int | None = None,
        limit: int = 100,
        **_kwargs: Any,
    ) -> tuple[list[dict[str, Any]], int | None]:
        items = [
            dict(x)
            for x in self.operations.values()
            if x["worker_id"] == worker_id
            and x["phase"] == "acquired"
            and (after_version_id is None or x["version_id"] > after_version_id)
        ]
        return items[:limit], None


@pytest.mark.parametrize("resume", [False, True])
def test_deleted_adapter_cleanup_does_not_require_rebuild_proof(
    tmp_path: Path, resume: bool
) -> None:
    cache, lifecycle, policy_manager = _manager(tmp_path, CachePolicy())
    identity, digest = _ready(cache, lifecycle)
    manager = policy_manager.deletion
    client = CleanupClient()
    manager.client = client  # type: ignore[assignment]
    facts = lifecycle.lifecycle("11-13")
    assert facts["rebuildability"] == "unknown"
    ordinary = DeletionEligibility(11, 13, identity, digest, facts["last_used_at"])
    with pytest.raises(CacheError) as blocked:
        manager.begin(ordinary)
    assert blocked.value.code == "cache_rebuild_unknown"
    assert client.operations == {}
    cleanup = replace(
        ordinary,
        cleanup_context={"cleanup_id": 17, "claim_attempt": 1},
        observed_identity={
            "store_id": lifecycle.owner(7)["store_id"],
            "language": "python",
            "source_sha256": identity["source_sha256"],
            "digest": digest,
        },
    )
    result = manager.begin(cleanup, max_nodes=1 if resume else 100_000)
    if resume:
        assert result.status == "in_progress"
        # A new coordinator consumes the durable record; no process-local exemption.
        manager = CacheDeletionManager(
            cache, lifecycle, client, worker_id=7, journal_protected=lambda _key: False
        )
        manager.recover_round(max_seconds=10)
    assert not cache.entry_path("11-13").exists()
    assert manager.cleanup_state(17) == "clear"
    assert all(x["phase"] == "completed" for x in client.operations.values())
    assert cache.accounting_snapshot()["committed_bytes"] == 0


def test_deleted_adapter_cleanup_preserves_pin_and_active_use(tmp_path: Path) -> None:
    cache, lifecycle, policy_manager = _manager(tmp_path, CachePolicy())
    identity, digest = _ready(cache, lifecycle)
    manager = policy_manager.deletion
    client = CleanupClient()
    manager.client = client  # type: ignore[assignment]
    lifecycle.confirm_rebuildability(
        "11-13",
        identity=identity,
        digest=digest,
        source_policy="verified_offline",
        evidence_note="synthetic local material",
        actor="test",
        valid_until=time.time() + 300,
    )
    eligibility = DeletionEligibility(
        11,
        13,
        identity,
        digest,
        lifecycle.lifecycle("11-13")["last_used_at"],
        cleanup_context={"cleanup_id": 17, "claim_attempt": 1},
        observed_identity={
            "store_id": lifecycle.owner(7)["store_id"],
            "language": "python",
            "source_sha256": identity["source_sha256"],
            "digest": digest,
        },
    )
    use = lifecycle.begin_use("11-13", worker_id=7, execution_id=19, attempt_id=23, fencing_token=1)
    with use:
        with pytest.raises(CacheError) as active:
            manager.begin(eligibility)
        assert active.value.code == "cache_entry_in_use"
        assert cache.entry_path("11-13").exists()
        assert client.operations == {}
        use.release(cleanup_completed=True)
    lifecycle.set_pin(
        "11-13", identity=identity, digest=digest, pinned=True, actor="test", reason="keep"
    )
    eligibility = replace(
        eligibility, last_used_before=lifecycle.lifecycle("11-13")["last_used_at"]
    )
    with pytest.raises(CacheError) as pinned:
        manager.begin(eligibility)
    assert pinned.value.code == "cache_pinned"
    assert cache.entry_path("11-13").exists()
    assert client.operations == {}
