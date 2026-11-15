"""The standalone Worker must keep uncertain local responsibility."""

import threading
from types import SimpleNamespace
from uuid import uuid4

from dlr.worker import agent_v4, broker_config_v4, cgroup_namespace, sandbox


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


def test_explicit_recovery_cannot_use_empty_roots_as_normal_startup(tmp_path, monkeypatch):
    """Mode/authorization gate only; synthetic bootstrap proves no physical stop."""
    monkeypatch.setattr(agent_v4, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(sandbox.SandboxConfig, "from_environment", lambda: object())
    monkeypatch.setattr(cgroup_namespace, "prepare", lambda config: config)
    monkeypatch.setattr(sandbox, "_mount", lambda *_: None)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("broker_config_read")
        raise AssertionError("no normal startup before recovery qualification")

    monkeypatch.setattr(broker_config_v4.BrokerConfigV4, "from_private_file", forbidden)
    anchor = tmp_path / "journal"
    anchor.mkdir(mode=0o700)
    config = agent_v4.WorkerConfig(
        base_url="https://control.example",
        worker_id=1,
        worker_identity_id=uuid4(),
        credential_directory=tmp_path / "credential",
        proof_trust_file=tmp_path / "trust.json",
        runtime_root=tmp_path / "runtime",
        journal_anchor=anchor,
        cleanup_journal_root=anchor,
        attempt_journal_root=anchor / "attempt-v4",
        broker_config_file=tmp_path / "broker.json",
        recovery_operation=uuid4(),
    )
    assert agent_v4.run(config, threading.Event()) == "recovery_required"
    assert calls == []
