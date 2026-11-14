"""The standalone Worker must keep uncertain local responsibility."""

import threading
from types import SimpleNamespace
from uuid import uuid4

from dlr.worker import agent_v4


def test_worker_requires_prebuilt_anchor_and_retains_unknown_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_v4, "sys", SimpleNamespace(platform="linux"))
    anchor = tmp_path / "journal"
    runtime = tmp_path / "runtime"
    cleanup = anchor / "cleanup"
    attempts = anchor / "attempts"
    config = agent_v4.WorkerConfig(
        base_url="https://control.example",
        worker_id=1,
        worker_identity_id=uuid4(),
        credential_directory=tmp_path / "credential",
        proof_trust_file=tmp_path / "trust.json",
        runtime_root=runtime,
        journal_anchor=anchor,
        cleanup_journal_root=cleanup,
        attempt_journal_root=attempts,
    )
    assert agent_v4.run(config, threading.Event()) == "worker_unavailable"
    assert not anchor.exists() and not runtime.exists()

    anchor.mkdir(mode=0o700)
    attempts.mkdir(mode=0o700)
    unknown = attempts / "unresolved.json"
    unknown.write_text("unknown responsibility")
    assert agent_v4.run(config, threading.Event()) == "recovery_required"
    assert unknown.read_text() == "unknown responsibility"
    assert cleanup.is_dir() and attempts.is_dir()
