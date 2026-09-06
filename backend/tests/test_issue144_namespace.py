"""Regression boundaries for nsdelegate bootstrap, budgets and restart ownership."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from dlr.worker import agent as worker_agent
from dlr.worker import cgroup_namespace, sandbox
from dlr.worker import workspace as workspace_manager
from dlr.worker.client import ControlClient

BOOT = "11111111-1111-1111-1111-111111111111"


def _limits(path: Path, *, cpu: str = "150000 100000", memory: int = 2 << 30) -> Path:
    path.mkdir()
    for name, value in {
        "cpu.max": cpu,
        "memory.max": str(memory),
        "memory.swap.max": "0",
        "pids.max": "512",
        "cgroup.controllers": "cpu memory pids",
        "cgroup.subtree_control": "cpu memory pids",
        "cgroup.procs": "",
        "cgroup.kill": "",
    }.items():
        (path / name).write_text(value)
    return path


def test_two_containers_count_their_own_limits_once(tmp_path):
    parent = _limits(tmp_path / "parent", cpu="350000 100000", memory=5 << 30)
    (parent / "pids.max").write_text("max")
    first = _limits(parent / ("a" * 64))
    second = _limits(parent / ("b" * 64))
    keeper = parent / "agent"
    keeper.mkdir()
    cgroup_namespace._verify_allocations(parent, [first, second, keeper])
    # Sharing a 3-CPU parent cannot give each 1.5 CPU plus a host reserve.
    (parent / "cpu.max").write_text("300000 100000")
    with pytest.raises(sandbox.SandboxError, match="Sandbox") as raised:
        cgroup_namespace._verify_allocations(parent, [first, second, keeper])
    assert raised.value.code == "sandbox_shared_parent_overcommitted"


@pytest.mark.parametrize(
    ("filename", "value"),
    [
        ("cpu.max", "max 100000"),
        ("memory.max", "max"),
        ("memory.swap.max", "max"),
        ("pids.max", "max"),
    ],
)
def test_unbounded_container_cannot_borrow_entire_parent(tmp_path, filename, value):
    parent = _limits(tmp_path / "parent", cpu="300000 100000", memory=3 << 30)
    child = _limits(parent / ("a" * 64))
    (child / filename).write_text(value)
    with pytest.raises(sandbox.SandboxError):
        cgroup_namespace._verify_allocations(parent, [child])


def test_instance_locks_cover_shared_journal_and_release_on_exit(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    journal = tmp_path / "journal"
    with cgroup_namespace.lock_roots(first, [journal]):
        with (
            pytest.raises(sandbox.SandboxError) as raised,
            cgroup_namespace.lock_roots(second, [journal]),
        ):
            pytest.fail("shared journal must not have two owners")
        assert raised.value.code == "sandbox_instance_root_in_use"
    with cgroup_namespace.lock_roots(second, [journal]):
        assert (journal / ".dlr-instance.lock").stat().st_mode & 0o777 == 0o600


def test_instance_lock_rejects_symlink_without_touching_target(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o711)
    sentinel = tmp_path / "keep"
    sentinel.write_text("sentinel")
    (root / ".dlr-instance.lock").symlink_to(sentinel)
    with pytest.raises(OSError), cgroup_namespace.lock_roots(root, []):
        pytest.fail("symlink must not be followed")
    assert sentinel.read_text() == "sentinel"


@pytest.fixture
def recovery_case(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_filesystem_magic", lambda path: sandbox.CGROUP2_SUPER_MAGIC)
    root = _limits(tmp_path / "cgroup")
    root_info = root.stat()
    identity = sandbox.NamespaceIdentity(
        BOOT,
        30,
        100,
        root_info.st_dev,
        root_info.st_ino,
        frozenset({(root_info.st_dev, root_info.st_ino)}),
    )
    config = sandbox.SandboxConfig(cgroup_path=root, namespace_identity=identity)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o711)
    workspaces = runtime / "workspaces"
    workspaces.mkdir(mode=0o700)
    attempt = workspaces / "attempt-22"
    attempt.mkdir(mode=0o700)
    mount = attempt / ".dlr-sandbox-mount"
    mount.mkdir(mode=0o700)
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    marker = journal / "sandbox-attempt-11-22.json"
    value = {
        "cgroup_name": "attempt-11-22",
        "execution_id": 11,
        "mount_name": mount.name,
        "mount_path": str(mount),
        "namespace_identity": {**identity.marker(), "root_inode": 1234},
        "cgroup_device": 30,
        "cgroup_inode": 1235,
    }
    marker.write_text(json.dumps(value))
    marker.chmod(0o600)
    return config, runtime, journal, marker, mount, value


def test_recreated_namespace_cleans_proven_absent_old_root_idempotently(recovery_case):
    config, runtime, journal, marker, mount, _ = recovery_case
    assert sandbox.recover(config, journal, runtime_root=runtime) == {
        "inspected": 1,
        "completed": 1,
        "retained": 0,
    }
    assert not marker.exists() and not mount.exists()
    assert sandbox.recover(config, journal, runtime_root=runtime) == {
        "inspected": 0,
        "completed": 0,
        "retained": 0,
    }


@pytest.mark.parametrize("reason", ["old_root_alive", "different_parent", "name_reused", "legacy"])
def test_restart_never_cleans_unproven_or_reused_kernel_objects(recovery_case, reason, monkeypatch):
    config, runtime, journal, marker, mount, value = recovery_case
    identity = config.namespace_identity
    assert identity is not None and config.cgroup_path is not None
    if reason == "old_root_alive":
        config = replace(
            config,
            namespace_identity=replace(
                identity,
                ancestor_children=identity.ancestor_children | {(identity.root_device, 1234)},
            ),
        )
    elif reason == "different_parent":
        value["namespace_identity"]["parent_inode"] = 999
    elif reason == "name_reused":
        child = _limits(config.cgroup_path / "attempt-11-22")
        (child / "cgroup.procs").write_text(str(os.getpid()))
    else:
        for field in ("namespace_identity", "cgroup_device", "cgroup_inode"):
            del value[field]
    marker.write_text(json.dumps(value))
    monkeypatch.setattr(
        sandbox, "_write", lambda *args: pytest.fail("must not kill unproven group")
    )
    assert sandbox.recover(config, journal, runtime_root=runtime) == {
        "inspected": 1,
        "completed": 0,
        "retained": 1,
    }
    assert marker.exists() and mount.exists()


def test_same_namespace_rejects_reused_attempt_inode(recovery_case, monkeypatch):
    config, runtime, journal, marker, mount, value = recovery_case
    assert config.namespace_identity is not None and config.cgroup_path is not None
    value["namespace_identity"] = config.namespace_identity.marker()
    child = _limits(config.cgroup_path / "attempt-11-22")
    value["cgroup_device"] = child.stat().st_dev
    value["cgroup_inode"] = child.stat().st_ino + 1
    marker.write_text(json.dumps(value))
    monkeypatch.setattr(sandbox, "_write", lambda *args: pytest.fail("must not kill reused inode"))
    assert sandbox.recover(config, journal, runtime_root=runtime)["retained"] == 1
    assert marker.exists() and mount.exists() and child.exists()


def test_background_retry_excludes_new_attempts_and_never_repeats_kernel_recovery(
    tmp_path, monkeypatch
):
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    old_name = "execution-11-attempt-22.cleanup.json"
    new_name = "execution-12-attempt-23.cleanup.json"
    (journal / old_name).write_text("old")
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(journal))
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    config = worker_agent.WorkerConfig()
    instance = worker_agent.Agent(config, ControlClient("https://example.invalid", "test-token"))
    kernel_calls = []
    retry_candidates = []

    def recover_kernel(*args, **kwargs):
        kernel_calls.append(True)
        return {"inspected": 0, "completed": 0, "retained": 0}

    def recover_files(*args, **kwargs):
        retry_candidates.append(kwargs["candidate_names"])
        return {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}

    monkeypatch.setattr(sandbox, "recover", recover_kernel)
    monkeypatch.setattr(workspace_manager, "recover_cleanup_journals", recover_files)
    instance._recover_cleanup_journals(7)
    (journal / new_name).write_text("live")
    instance._recover_cleanup_journals(7, startup=False)
    assert len(kernel_calls) == 1
    assert retry_candidates == [frozenset({old_name}), frozenset({old_name})]
    assert (journal / new_name).read_text() == "live"


def test_unresolved_sandbox_blocks_workspace_receipts_and_consumption(tmp_path, monkeypatch):
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = worker_agent.WorkerConfig()
    config.isolation_capabilities = dict.fromkeys(worker_agent.ISOLATION_CAPABILITY_KEYS, True)
    instance = worker_agent.Agent(config, ControlClient("https://example.invalid", "test-token"))
    monkeypatch.setattr(
        sandbox,
        "recover",
        lambda *args, **kwargs: {
            "inspected": 1,
            "completed": 0,
            "retained": 1,
        },
    )
    monkeypatch.setattr(
        workspace_manager,
        "recover_cleanup_journals",
        lambda *args, **kwargs: pytest.fail(
            "must not clean files or acknowledge while cgroup ownership is unresolved"
        ),
    )
    instance._recover_cleanup_journals(7)
    instance._recover_cleanup_journals(7, startup=False)
    assert instance._sandbox_recovery_blocked
    assert not any(config.isolation_capabilities.values())


def test_workspace_candidate_filter_does_not_inspect_a_live_journal(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    live = journal / "execution-12-attempt-23.cleanup.json"
    live.write_text("live")
    monkeypatch.setattr(
        workspace_manager,
        "_load_journal",
        lambda *args, **kwargs: pytest.fail(
            "live journals must be excluded before validation or mutation"
        ),
    )
    counts = workspace_manager.recover_cleanup_journals(
        journal,
        tmp_path / "runtime",
        candidate_names={"execution-11-attempt-22.cleanup.json"},
    )
    assert counts == {"inspected": 0, "completed": 0, "deferred": 0, "retained": 0}
    assert live.read_text() == "live"
