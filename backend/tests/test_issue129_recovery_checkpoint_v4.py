"""Private checkpoint schema and interrupted publication, not physical stop proof."""

import copy
import json
import os
import stat
from uuid import uuid4

import pytest

from dlr.worker import journal_v4, sandbox
from dlr.worker import recovery_checkpoint_v4 as checkpoint


def _prepared():
    limits = sandbox.ResourceLimits(1.0, 512 << 20, 128, 64 << 20, 256, 30)
    roots = {
        key: {"device": 7, "inode": index}
        for index, key in enumerate(("runtime", "anchor", "attempts"), start=10)
    }
    boot = str(uuid4())
    return {
        "version": 1,
        "phase": "prepared",
        "worker_id": 3,
        "worker_identity_id": str(uuid4()),
        "instance_id": str(uuid4()),
        "instance_generation": 2,
        "execution_id": 5,
        "attempt_id": 8,
        "attempt_no": 1,
        "fencing_token": 6,
        "boot_id": boot,
        "roots": roots,
        "storage_id": checkpoint.digest({"boot_id": boot, "roots": roots}),
        "dispatch": {
            "schema_version": 1,
            "message_id": str(uuid4()),
            "execution_id": 5,
            "dispatch_generation": 1,
            "adapter_id": 4,
            "language": "python",
            "resource_class": "standard",
            "target_worker_id": 3,
        },
        "receive_charge": checkpoint._charge(sandbox.ResourceReservation("receive", limits)),
        "task_charge": checkpoint._charge(sandbox.ResourceReservation("task", limits)),
        "journal_records": {"attempt-8.attempt.json": "1" * 64},
        "runtime_record": {"claim-123.pending.json": "2" * 64},
        "objects": {
            key: {"device": 7, "inode": index}
            for index, key in enumerate(("workspaces", "parent", "workspace", "cgroup"), start=20)
        },
        "process": {
            "start_ticks": 100,
            "pid_namespace": {"device": 9, "inode": 10},
            "mount_namespace": {"device": 9, "inode": 11},
        },
    }


def _stopped(prepared):
    return {
        "version": 1,
        "phase": "stopped",
        "attempt_id": 8,
        "prepared_sha256": checkpoint.digest(prepared),
        "intent_id": str(uuid4()),
        "body_mac": "3" * 64,
        "result_record": {"result-8-123.intent.json": "4" * 64},
        "stop_observation": "cgroup_empty_supervisor_reaped",
        "workspace_entries": {},
        "sandbox_mount_expected": True,
        "sandbox_mount_binding": None,
    }


@pytest.mark.parametrize("failed", [False, True])
def test_file_v3_requires_explicit_file_context_and_exact_input_binding(failed):
    from dlr.worker import recovery_local_v4

    prepared = _prepared()
    prepared.update(
        version=3,
        input_directory={"device": 7, "inode": 30},
        input_files=[]
        if failed
        else [
            {
                "mount_name": "input-00.txt",
                "size_bytes": 0,
                "sha256": "0" * 64,
                "device": 7,
                "inode": 31,
            }
        ],
        preparation_failed=failed,
    )
    stopped = _stopped(prepared)
    stopped.update(version=3, stop_reason="preparation_failed" if failed else None)
    stopped["workspace_entries"]["input"] = prepared["input_directory"]
    assert not checkpoint.valid_prepared(prepared)
    assert not checkpoint.valid_stopped(stopped, prepared)
    assert checkpoint.valid_prepared(prepared, _active_files=True)
    assert checkpoint.valid_stopped(stopped, prepared, _active_files=True)
    terminal = {
        "version": 3,
        "prepared": prepared,
        "stopped": stopped,
        "attempt_record": {},
        "finalization_id": str(uuid4()),
        "completion_id": str(uuid4()),
        "records": {},
        "runtime_records": {},
    }
    assert not recovery_local_v4.valid_terminal(terminal)
    assert recovery_local_v4.valid_terminal(terminal, _active_files=True)
    stopped["stop_reason"] = None if failed else "preparation_failed"
    assert not checkpoint.valid_stopped(stopped, prepared, _active_files=True)


def test_file_checkpoint_requires_exact_readonly_original_objects(tmp_path):
    from types import SimpleNamespace

    (tmp_path / "input").mkdir(mode=0o700)
    path = tmp_path / "input" / "input-00.txt"
    path.write_bytes(b"file")
    path.chmod(0o444)
    path.parent.chmod(0o555)
    info = path.stat()
    root = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    staged = SimpleNamespace(
        prepared=SimpleNamespace(managed_files=True),
        preparation_failed=False,
        workspace=SimpleNamespace(_leaf=root),
        input_files=[
            {
                "mount_name": path.name,
                "size_bytes": 4,
                "sha256": "0" * 64,
                "device": info.st_dev,
                "inode": info.st_ino,
            }
        ],
    )
    try:
        os.close(checkpoint.open_file_input(staged))
        # Same name and bytes cannot replace the frozen object binding.
        path.parent.chmod(0o700)
        replacement = path.parent / "replacement"
        replacement.write_bytes(b"file")
        replacement.chmod(0o444)
        replacement.replace(path)
        path.parent.chmod(0o555)
        with pytest.raises(ValueError, match="file checkpoint unavailable"):
            checkpoint.open_file_input(staged)
    finally:
        path.parent.chmod(0o700)
        os.close(root)


def test_new_records_bind_both_frozen_charges_and_original_dispatch():
    prepared = _prepared()
    assert checkpoint.valid_prepared(prepared)
    stopped = _stopped(prepared)
    assert checkpoint.valid_stopped(stopped, prepared)
    prepared["receive_charge"]["limits"]["memory_bytes"] += 1
    assert checkpoint.valid_prepared(prepared)
    assert not checkpoint.valid_stopped(stopped, prepared)
    assert not checkpoint.valid_prepared({"protocol_version": 4, "journal_version": 2})


@pytest.mark.parametrize(
    "language,entries",
    [
        ("python", ["adapter.py"]),
        ("python", [".dlr-harness.py", "input.json"]),
        ("javascript", ["adapter.mjs"]),
        ("javascript", [".dlr-harness.mjs", "runtime_config.json"]),
        ("javascript", ["input", "temp", "output.json"]),
    ],
)
def test_stopped_keeps_actual_single_language_subsets_and_old_python_records(language, entries):
    prepared = _prepared()
    prepared["dispatch"]["language"] = language
    stopped = _stopped(prepared)
    stopped["workspace_entries"] = {
        name: {"device": 7, "inode": 30 + i} for i, name in enumerate(entries)
    }
    assert checkpoint.valid_stopped(stopped, prepared)
    assert stopped["version"] == prepared["version"] == 1


@pytest.mark.parametrize(
    "language,entries",
    [
        ("python", ["adapter.mjs"]),
        ("javascript", ["adapter.py"]),
        ("javascript", [".dlr-harness.py", "adapter.mjs"]),
        ("python", [".dlr-harness.py", ".dlr-harness.mjs"]),
        ("javascript", ["package.json"]),
        ("javascript", ["node_modules"]),
        ("javascript", ["../adapter.mjs"]),
        ("typescript", ["package.json"]),
        ("go", ["go-business-binary"]),
    ],
)
def test_language_does_not_widen_checkpoint_inventory_or_recovery_qualification(language, entries):
    prepared = _prepared()
    prepared["dispatch"]["language"] = language
    stopped = _stopped(prepared)
    stopped["workspace_entries"] = {
        name: {"device": 7, "inode": 30 + i} for i, name in enumerate(entries)
    }
    assert not checkpoint.valid_stopped(stopped, prepared)
    assert not checkpoint.valid_stopped(None, prepared)


@pytest.mark.parametrize(
    "key,value",
    [
        ("version", True),
        ("phase", "stopped"),
        ("attempt_id", True),
        ("worker_identity_id", str(uuid4()).upper()),
        ("boot_id", str(uuid4()).upper()),
        ("instance_id", "00000000-0000-0000-0000-000000000000"),
        ("storage_id", "0" * 64),
        ("roots", {}),
        ("objects", {}),
        ("receive_charge", {}),
        ("task_charge", {}),
        ("runtime_record", {}),
        ("journal_records", {"../outside.json": "0" * 64}),
    ],
)
def test_invalid_or_old_provenance_never_gets_recovery_qualification(key, value):
    record = _prepared()
    record[key] = value
    assert not checkpoint.valid_prepared(record)


def test_extra_authority_and_unknown_accounting_are_rejected():
    record = _prepared()
    record["claim_token"] = "must-not-be-persisted"
    assert not checkpoint.valid_prepared(record)
    record = _prepared()
    record["receive_charge"]["accounting_version"] = 1
    assert not checkpoint.valid_prepared(record)
    record = _prepared()
    record["task_charge"]["cpu_units"] = 1
    assert not checkpoint.valid_prepared(record)
    record = _prepared()
    record["dispatch"]["execution_id"] = 9
    assert not checkpoint.valid_prepared(record)


def test_stopped_provenance_rejects_changed_result_and_unknown_workspace():
    prepared = _prepared()
    stopped = _stopped(prepared)
    for key, value in (
        ("intent_id", "not-an-id"),
        ("prepared_sha256", "0" * 64),
        ("body_mac", "x" * 64),
        ("sandbox_mount_expected", 1),
        ("workspace_entries", {"extra-dir": {"device": 7, "inode": 8}}),
    ):
        changed = copy.deepcopy(stopped)
        changed[key] = value
        assert not checkpoint.valid_stopped(changed, prepared)


def test_private_record_publication_and_directory_sync_retry(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    record = _prepared()
    name = checkpoint.names(8)[0]
    with journal_v4._locked_root(root) as fd:
        fsync = os.fsync
        failed = []

        def fail_directory_sync(descriptor):
            if descriptor == fd and not failed:
                failed.append(True)
                raise OSError("injected directory sync failure")
            fsync(descriptor)

        monkeypatch.setattr(os, "fsync", fail_directory_sync)
        with pytest.raises(OSError):
            checkpoint.persist(fd, name, record)
        assert journal_v4._read(fd, name) == record
        checkpoint.persist(fd, name, record)
        assert failed and stat.S_IMODE((root / name).stat().st_mode) == 0o600
        assert set(json.loads((root / name).read_text())) == set(record)


@pytest.mark.parametrize("damage", ["truncated", "duplicate", "hardlink", "permissions"])
def test_unknown_record_is_retained_and_never_replaced(tmp_path, damage):
    root = tmp_path / "journal"
    root.mkdir(mode=0o700)
    record, name = _prepared(), checkpoint.names(8)[0]
    with journal_v4._locked_root(root) as fd:
        checkpoint.persist(fd, name, record)
        target = root / name
        if damage == "truncated":
            target.write_text('{"version":')
        elif damage == "duplicate":
            target.write_text('{"version":1,"version":1}')
        elif damage == "hardlink":
            os.link(target, root / "retained.json")
        else:
            target.chmod(0o644)
        before = target.read_bytes()
        with pytest.raises(ValueError):
            checkpoint.persist(fd, name, record)
        assert target.read_bytes() == before


def test_synthetic_owners_cannot_manufacture_stop_checkpoint():
    with pytest.raises(ValueError):
        checkpoint.persist_stopped(object(), object(), {}, "result.json")


def test_new_pid_one_can_have_reused_namespace_inode_numbers():
    original = _prepared()["process"]
    current = copy.deepcopy(original)
    current["start_ticks"] += 1
    assert checkpoint.fresh_process(original, current)
    assert not checkpoint.fresh_process(original, original)
    current["start_ticks"] = original["start_ticks"] - 1
    assert not checkpoint.fresh_process(original, current)
    current["start_ticks"] = True
    assert not checkpoint.fresh_process(original, current)
