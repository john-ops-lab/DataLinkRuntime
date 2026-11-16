"""The standalone Worker must keep uncertain local responsibility."""

import json
import os
import socket
import struct
import sys
import threading
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dlr.control.schemas.reliable_runtime import V4TaskPayload
from dlr.worker import (
    agent_v4,
    broker_config_v4,
    cgroup_namespace,
    claim_task_child_v4,
    node_runtime_v4,
    sandbox,
    task_fingerprint_v4,
)
from worker_runtime_support import attempt_payload


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
    for mode in ("javascript", "python_js", "", "PYTHON"):
        assert agent_v4.run(replace(config, language_mode=mode), threading.Event()) == (
            "configuration_invalid"
        )
    assert not anchor.exists() and not runtime.exists()
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


def test_unavailable_fixed_node_cannot_emit_a_successful_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(node_runtime_v4, "NODE_EXECUTABLE", str(tmp_path / "missing-node"))
    assert not node_runtime_v4.run_preflight(
        sandbox.SandboxConfig(cgroup_path=None),
        recovery_root=tmp_path / "journal",
        runtime_root=tmp_path / "runtime",
    )
    assert not (tmp_path / "runtime").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="sealed launch requires Linux memfd")
@pytest.mark.parametrize("language", ["python", "javascript"])
@pytest.mark.parametrize("tamper", [None, "fingerprint", "directory", "bytes"])
def test_bounded_stage_uses_same_task_directory_and_sealed_minimal_launch(
    tmp_path, monkeypatch, language, tamper
):
    """Real staging/SCM_RIGHTS/memfd only; physical isolation is G2/G4."""
    code = (
        "export async function handle(c, input) { return input; }"
        if language == "javascript"
        else "def handle(c, input):\n    return input\n"
    )
    task = V4TaskPayload.model_validate(
        attempt_payload(
            {
                "protocol_version": 4,
                "execution_id": 1,
                "attempt_id": 2,
                "adapter_id": 3,
                "version_id": 4,
                "latest_version_id": 4,
                "worker_identity_id": str(uuid4()),
                "instance_id": str(uuid4()),
                "instance_generation": 1,
                "language": language,
                "code": code,
                "input": {"value": "中文"},
                "input_source_type": "json",
                "input_snapshot": {"source_type": "json"},
            }
        )
    )
    fingerprint = task_fingerprint_v4.fingerprint(task, task.claim_token).hex()
    workspace = tmp_path / "attempt-2" / "dlr-exec-1"
    workspace.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("NODE_OPTIONS", "--require=/untrusted/file")
    monkeypatch.setenv("NODE_PATH", "/untrusted/modules")
    monkeypatch.setenv("NPM_CONFIG_PREFIX", "/untrusted")
    parent, child = socket.socketpair()
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        command = {
            "op": "stage",
            "workspace": str(workspace),
            "workspace_device": info.st_dev,
            "workspace_inode": info.st_ino,
            "staging_bytes": 1 << 20,
            "launch_bytes": 65536,
            "fingerprint": fingerprint,
        }
        if tamper == "fingerprint":
            task.input = {"value": "changed"}
        elif tamper == "directory":
            command["workspace_inode"] += 1
        elif tamper == "bytes":
            command["staging_bytes"] = 1
        assert (
            parent.sendmsg(
                [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", directory))]
            )
            == 1
        )
        if tamper:
            with pytest.raises(ValueError, match="task staging unavailable"):
                claim_task_child_v4._stage_python(child, task, fingerprint, command)
            return
        claim_task_child_v4._stage_python(child, task, fingerprint, command)
        assert claim_task_child_v4._receive(parent)["fingerprint"] == fingerprint
        data, ancillary, flags, _ = parent.recvmsg(1, socket.CMSG_SPACE(struct.calcsize("i")))
        assert data == b"D" and flags == 0
        launch_fd = struct.unpack("i", ancillary[0][2])[0]
        try:
            argv, environment = sandbox._read_sealed_launch(launch_fd, 65536)
        finally:
            os.close(launch_fd)
        suffix = "mjs" if language == "javascript" else "py"
        assert (workspace / f"adapter.{suffix}").read_text() == code
        assert json.loads((workspace / "input.json").read_text()) == {"value": "中文"}
        assert set(environment) == {"PATH"}
        if language == "javascript":
            assert argv == [
                "/usr/local/bin/node",
                str(workspace / ".dlr-harness.mjs"),
                str(workspace),
                str(workspace / "adapter.mjs"),
            ]
            assert environment == {"PATH": "/usr/bin:/bin"}
        else:
            assert argv == [
                sys.executable,
                "-I",
                str(workspace / ".dlr-harness.py"),
                str(workspace),
            ]
    finally:
        os.close(directory)
        parent.close()
        child.close()
