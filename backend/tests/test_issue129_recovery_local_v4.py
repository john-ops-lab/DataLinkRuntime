"""Real local unlink/fsync interruptions; namespace stubs grant no host proof."""

import os
from uuid import uuid4

import pytest

from dlr.worker import cgroup_namespace, journal_anchor_v4, journal_v4, workspace_fs_v4
from dlr.worker import recovery_checkpoint_v4 as checkpoint
from dlr.worker import recovery_execution_v4 as recovery
from dlr.worker import recovery_local_v4 as local
from test_issue129_recovery_checkpoint_v4 import _prepared, _stopped


@pytest.fixture
def original(tmp_path, monkeypatch):
    # macOS lacks Linux mountinfo. These tests validate actual file operations
    # only; actual mount/namespace rejection is a separate Linux acceptance.
    monkeypatch.setattr(workspace_fs_v4, "_mounts_safe", lambda *_: True)
    runtime, anchor = tmp_path / "runtime", tmp_path / "journal"
    attempts = anchor / "attempt-v4"
    runtime.mkdir(mode=0o711)
    anchor.mkdir(mode=0o700)
    workspaces = runtime / "workspaces"
    parent, leaf = workspaces / "attempt-8", workspaces / "attempt-8" / "dlr-exec-5"
    for path in (workspaces, parent, leaf, leaf / "input", leaf / "temp", leaf / "output"):
        path.mkdir(mode=0o700)
    for name in ("adapter.py", "input.json"):
        (leaf / name).write_text("{}")
        (leaf / name).chmod(0o600)
    with (
        journal_anchor_v4.prepare_roots(
            anchor, runtime_root=runtime, journal_roots=(anchor, attempts)
        ) as owner,
        cgroup_namespace.lock_roots_v4(runtime, [anchor, attempts]) as lock,
    ):
        owner.bind(lock)
        fd, runtime_fd = checkpoint._root_fd(lock, attempts), checkpoint._root_fd(lock, runtime)
        prepared = _prepared()
        prepared["roots"] = {
            "runtime": checkpoint.binding(runtime_fd),
            "anchor": checkpoint.binding(owner.fd),
            "attempts": checkpoint.binding(fd),
        }
        prepared["storage_id"] = checkpoint.digest(
            {"boot_id": prepared["boot_id"], "roots": prepared["roots"]}
        )
        for key, path in (("workspaces", workspaces), ("parent", parent), ("workspace", leaf)):
            info = path.stat()
            prepared["objects"][key] = {"device": info.st_dev, "inode": info.st_ino}
        attempt_record = {
            "protocol_version": 4,
            "journal_version": 2,
            "namespace_identity": {
                "boot_id": prepared["boot_id"],
                "parent_device": 1,
                "parent_inode": 2,
                "root_device": 1,
                "root_inode": 3,
            },
            **{
                key: prepared[key]
                for key in (
                    "worker_identity_id",
                    "instance_id",
                    "instance_generation",
                    "execution_id",
                    "attempt_id",
                    "attempt_no",
                    "fencing_token",
                )
            },
            "cleanup_token": "test_cleanup_token",
            "workspace_path": str(leaf),
            "lease_expires_at": "2026-11-15T03:00:00Z",
        }
        assert journal_v4.valid_record(attempt_record, "attempt")
        attempt_name = journal_v4._name(attempt_record, "attempt")
        checkpoint.persist(fd, attempt_name, attempt_record)
        pending = {"version": 1, "attempt_id": 8}
        checkpoint.persist(runtime_fd, "claim-test.pending.json", pending)
        prepared["journal_records"] = {attempt_name: checkpoint.digest(attempt_record)}
        prepared["runtime_record"] = {"claim-test.pending.json": checkpoint.digest(pending)}
        stopped = _stopped(prepared)
        result = {"version": 1, "intent_id": stopped["intent_id"], "body_mac": stopped["body_mac"]}
        checkpoint.persist(fd, "result-8-test.intent.json", result)
        stopped["result_record"] = {"result-8-test.intent.json": checkpoint.digest(result)}
        stopped["workspace_entries"] = {
            path.name: {"device": path.stat().st_dev, "inode": path.stat().st_ino}
            for path in leaf.iterdir()
        }
        for name, record in zip(checkpoint.names(8), (prepared, stopped), strict=True):
            checkpoint.persist(fd, name, record)
        yield lock, runtime, attempts, prepared, stopped, attempt_record, fd, runtime_fd


def scan(original):
    lock, runtime, attempts, prepared, *_ = original
    from uuid import UUID

    return recovery.scan(
        lock=lock,
        runtime=runtime,
        attempts=attempts,
        worker_id=prepared["worker_id"],
        identity_id=UUID(prepared["worker_identity_id"]),
        boot_id=prepared["boot_id"],
    )


def test_read_only_scan_retains_both_charges_and_rejects_missing_without_intent(original):
    state = scan(original)
    assert state.prepared["receive_charge"] and state.prepared["task_charge"]
    leaf = original[1] / "workspaces/attempt-8/dlr-exec-5"
    (leaf / "adapter.py").unlink()
    before = set(original[2].iterdir())
    with pytest.raises(ValueError, match="missing without intent"):
        scan(original)
    assert set(original[2].iterdir()) == before
    assert (leaf / "input.json").exists()


@pytest.mark.parametrize("damage", ["replace", "extra", "nonempty", "symlink", "foreign_attempt"])
def test_read_only_scan_refuses_changed_objects_before_registration(original, damage):
    leaf = original[1] / "workspaces/attempt-8/dlr-exec-5"
    if damage == "replace":
        fresh = leaf / "replacement"
        fresh.write_text("{}")
        fresh.chmod(0o600)
        os.replace(fresh, leaf / "adapter.py")
    elif damage == "extra":
        (leaf / "extra").mkdir()
    elif damage == "nonempty":
        (leaf / "input" / "unknown").write_text("unowned")
    elif damage == "foreign_attempt":
        (leaf.parent.parent / "attempt-9").mkdir()
    else:
        (leaf / "adapter.py").unlink()
        (leaf / "adapter.py").symlink_to("input.json")
    with pytest.raises(ValueError):
        scan(original)
    assert leaf.exists()


@pytest.mark.parametrize("interruption", range(7))
def test_each_physical_unlink_or_rmdir_can_reenter(original, monkeypatch, interruption):
    _, runtime, _, prepared, stopped, _, directory, runtime_fd = original
    unlink, rmdir = os.unlink, os.rmdir
    mutations = []

    def fault(operation, name, **kwargs):
        operation(name, **kwargs)
        mutations.append(name)
        if len(mutations) == interruption + 1:
            raise OSError("injected after destructive operation")

    with monkeypatch.context() as m:
        m.setattr(os, "unlink", lambda name, **kw: fault(unlink, name, **kw))
        m.setattr(os, "rmdir", lambda name, **kw: fault(rmdir, name, **kw))
        with pytest.raises(OSError):
            local.clean_workspace(directory, runtime_fd, runtime, prepared, stopped)
    # Exact durable intents authorize only the prefix actually deleted.
    scan(original)
    local.clean_workspace(directory, runtime_fd, runtime, prepared, stopped)
    assert not (runtime / "workspaces/attempt-8").exists()
    assert mutations


def _terminal(original):
    _, runtime, _, prepared, stopped, attempt_record, directory, runtime_fd = original
    local.clean_workspace(directory, runtime_fd, runtime, prepared, stopped)
    records = {
        name: checkpoint.digest(journal_v4._read(directory, name))
        for name in local._entries(directory)
        if name != ".dlr-instance.lock"
    }
    return {
        "version": 1,
        "prepared": prepared,
        "stopped": stopped,
        "attempt_record": attempt_record,
        "finalization_id": str(uuid4()),
        "completion_id": str(uuid4()),
        "records": records,
        "runtime_records": prepared["runtime_record"],
    }


@pytest.mark.parametrize(
    "kind",
    ["attempt", "result", "prepared", "stopped", "physical", "delete", "runtime", "terminal"],
)
def test_terminal_survives_every_class_of_record_deletion(original, monkeypatch, kind):
    terminal = _terminal(original)
    directory, runtime_fd = original[-2:]
    unlink = os.unlink
    fired = []

    def interrupt(name, **kwargs):
        match = {
            "attempt": name.startswith("attempt-"),
            "result": name.startswith("result-"),
            "prepared": name.endswith(".prepared.json"),
            "stopped": name.endswith(".stopped.json"),
            "physical": name.endswith(".physical.json"),
            "delete": ".delete-" in name,
            "runtime": kwargs.get("dir_fd") == runtime_fd,
            "terminal": name.endswith(".terminal.json"),
        }[kind]
        if match and not fired:
            fired.append(name)
            if kind != "terminal":
                unlink(name, **kwargs)
            raise OSError("injected retirement interruption")
        unlink(name, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "unlink", interrupt)
        with pytest.raises(OSError):
            local.retire(directory, runtime_fd, terminal, allow_missing=False)
    assert fired
    assert scan(original).terminal == terminal
    local.retire(directory, runtime_fd, terminal, allow_missing=True)
    assert local._entries(directory) == {".dlr-instance.lock"}
    assert not (original[1] / "claim-test.pending.json").exists()


def test_terminal_conflict_keeps_original_responsibility(original):
    terminal = _terminal(original)
    directory, runtime_fd = original[-2:]
    checkpoint.persist(directory, local.terminal_name(8), terminal)
    record_name = next(iter(terminal["records"]))
    (original[2] / record_name).write_text('{"unknown":true}')
    with pytest.raises(ValueError, match="conflict"):
        local.retire(directory, runtime_fd, terminal, allow_missing=True)
    assert journal_v4._read(directory, local.terminal_name(8)) == terminal
    assert (original[1] / "claim-test.pending.json").exists()


def test_terminal_cannot_replace_physical_removal(original):
    prepared, stopped, attempt_record, directory = original[3:7]
    records = {
        name: checkpoint.digest(journal_v4._read(directory, name))
        for name in local._entries(directory)
        if name != ".dlr-instance.lock"
    }
    terminal = {
        "version": 1,
        "prepared": prepared,
        "stopped": stopped,
        "attempt_record": attempt_record,
        "finalization_id": str(uuid4()),
        "completion_id": str(uuid4()),
        "records": records,
        "runtime_records": prepared["runtime_record"],
    }
    checkpoint.persist(directory, local.terminal_name(8), terminal)
    with pytest.raises(ValueError, match="physical residue"):
        scan(original)
    assert (original[1] / "workspaces/attempt-8/dlr-exec-5/adapter.py").exists()
