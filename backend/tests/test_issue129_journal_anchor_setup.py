"""Host-side anchor preparation is explicit and never erases old records."""

import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/prepare-worker-journal-anchor.py"


@pytest.fixture
def setup_module(monkeypatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("dlr129_anchor_setup_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Host unit tests can run on macOS; actual mount identity is tested on Linux.
    if os.uname().sysname != "Linux":
        monkeypatch.setattr(module, "_mount_id", lambda fd: os.fstat(fd).st_dev)
    return module


def test_create_is_explicit_and_verify_preserves_existing_records(tmp_path, setup_module):
    parent = tmp_path.resolve()
    anchor = parent / "journal"
    assert setup_module.prepare_anchor(anchor, create=True) == "created_and_synced"
    assert stat.S_IMODE(anchor.stat().st_mode) == 0o700
    record = anchor / "old.pending.json"
    record.write_text("existing responsibility")
    with pytest.raises(setup_module.AnchorSetupError) as error:
        setup_module.prepare_anchor(anchor, create=True)
    assert error.value.code == "anchor_exists"
    assert setup_module.prepare_anchor(anchor, create=False) == "verified_and_synced"
    assert record.read_text() == "existing responsibility"


def test_verify_never_creates_missing_anchor_or_parent(tmp_path, setup_module):
    anchor = tmp_path.resolve() / "missing-parent" / "journal"
    with pytest.raises(setup_module.AnchorSetupError):
        setup_module.prepare_anchor(anchor, create=False)
    assert not anchor.parent.exists()


def test_symlink_in_host_path_is_rejected_without_touching_target(tmp_path, setup_module):
    parent = tmp_path.resolve()
    target = parent / "target"
    target.mkdir(mode=0o700)
    (parent / "alias").symlink_to(target, target_is_directory=True)
    with pytest.raises(setup_module.AnchorSetupError):
        setup_module.prepare_anchor(parent / "alias" / "journal", create=True)
    assert not (target / "journal").exists()


def test_insecure_existing_anchor_is_not_chmoded_or_overwritten(tmp_path, setup_module):
    anchor = tmp_path.resolve() / "journal"
    anchor.mkdir(mode=0o700)
    anchor.chmod(0o755)
    record = anchor / "record"
    record.write_text("preserve")
    with pytest.raises(setup_module.AnchorSetupError):
        setup_module.prepare_anchor(anchor, create=False)
    assert stat.S_IMODE(anchor.stat().st_mode) == 0o755
    assert record.read_text() == "preserve"


@pytest.mark.parametrize("failure", [OSError, MemoryError])
def test_sync_failure_is_unknown_and_leaves_created_directory(
    tmp_path, setup_module, monkeypatch, failure
):
    anchor = tmp_path.resolve() / "journal"
    original = setup_module._sync

    def fail_sync(_fd):
        raise failure("synthetic fsync failure")

    monkeypatch.setattr(setup_module, "_sync", fail_sync)
    with pytest.raises(setup_module.AnchorSetupError) as error:
        setup_module.prepare_anchor(anchor, create=True)
    assert error.value.code == "anchor_unconfirmed" and anchor.is_dir()
    monkeypatch.setattr(setup_module, "_sync", original)
    assert setup_module.prepare_anchor(anchor, create=False) == "verified_and_synced"


def test_invalid_created_mode_is_unknown_and_directory_is_not_repaired(
    tmp_path, setup_module, monkeypatch
):
    anchor = tmp_path.resolve() / "journal"
    original_mkdir = os.mkdir

    def create_wrong_mode(name, mode, *, dir_fd):
        original_mkdir(name, mode, dir_fd=dir_fd)
        os.chmod(name, 0o755, dir_fd=dir_fd)

    monkeypatch.setattr(setup_module.os, "mkdir", create_wrong_mode)
    with pytest.raises(setup_module.AnchorSetupError) as error:
        setup_module.prepare_anchor(anchor, create=True)
    assert error.value.code == "anchor_unconfirmed"
    assert stat.S_IMODE(anchor.stat().st_mode) == 0o755


def test_replaced_parent_during_sync_is_not_reported_success(tmp_path, setup_module, monkeypatch):
    root = tmp_path.resolve()
    parent = root / "original"
    parent.mkdir(mode=0o700)
    anchor = parent / "journal"
    original_sync = setup_module._sync
    replaced = []

    def replace_parent(fd):
        original_sync(fd)
        if not replaced:
            replaced.append(True)
            parent.rename(root / "moved")
            parent.mkdir(mode=0o700)

    monkeypatch.setattr(setup_module, "_sync", replace_parent)
    with pytest.raises(setup_module.AnchorSetupError) as error:
        setup_module.prepare_anchor(anchor, create=True)
    assert error.value.code == "anchor_unconfirmed"
    assert (root / "moved" / "journal").is_dir()
    assert not anchor.exists()


def test_replaced_existing_anchor_is_not_verified(tmp_path, setup_module, monkeypatch):
    parent = tmp_path.resolve()
    anchor = parent / "journal"
    anchor.mkdir(mode=0o700)
    (anchor / "record").write_text("existing responsibility")
    original_sync = setup_module._sync
    replaced = []

    def replace_anchor(fd):
        original_sync(fd)
        if not replaced:
            replaced.append(True)
            anchor.rename(parent / "old-journal")
            anchor.mkdir(mode=0o700)

    monkeypatch.setattr(setup_module, "_sync", replace_anchor)
    with pytest.raises(setup_module.AnchorSetupError) as error:
        setup_module.prepare_anchor(anchor, create=False)
    assert error.value.code == "anchor_unconfirmed"
    assert (parent / "old-journal" / "record").read_text() == "existing responsibility"


def test_changed_mount_identity_is_not_verified(tmp_path, setup_module, monkeypatch):
    anchor = tmp_path.resolve() / "journal"
    anchor.mkdir(mode=0o700)
    mounts = iter((10, 20, 11))  # parent, anchor, reopened parent
    monkeypatch.setattr(setup_module, "_mount_id", lambda _fd: next(mounts))
    with pytest.raises(setup_module.AnchorSetupError) as error:
        setup_module.prepare_anchor(anchor, create=False)
    assert error.value.code == "anchor_unconfirmed"
    assert anchor.is_dir()
