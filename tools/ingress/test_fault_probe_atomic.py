"""Local proof-file failure handling; no database or message service is used."""

import hashlib
import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fault_probe


def _compete_for_marker(root, barrier, results):
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        barrier.wait(timeout=5)
        try:
            fault_probe.Probe._exclusive_write(directory, "race-marker", b"winner")
            results.put("created")
        except FileExistsError:
            results.put("exists")
    finally:
        os.close(directory)


def _compete_for_cut(root, barrier, results):
    arm_path = Path(root) / "arm.json"
    os.environ.update(
        DLR_INGRESS_FAULT_ENABLED="1",
        DLR_INGRESS_FAULT_SCOPE="issue222-isolated",
        DLR_INGRESS_FAULT_ROLE="worker",
        DLR_INGRESS_FAULT_ARM=str(arm_path),
    )
    probe = fault_probe.Probe(arm_path, "worker")
    arm = probe.arm()
    if arm is None:
        raise RuntimeError("test_arm_unavailable")
    barrier.wait(timeout=5)
    result = probe.fire(
        arm,
        {"subscription_id": 11, "run_id": "fault-unit"},
        {},
        diagnostic_receipt=True,
    )
    results.put(result)


class ProofFileFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)

    def tearDown(self):
        os.close(self.directory)
        self.temp.cleanup()

    def test_failed_write_or_sync_removes_owned_partial_and_allows_retry(self):
        real_fdopen, real_fsync = os.fdopen, os.fsync
        for phase in ("write", "file_sync", "directory_sync"):
            with self.subTest(phase=phase):
                failed = False

                def sync(fd, *, phase=phase):
                    nonlocal failed
                    is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if not failed and ((phase == "directory_sync") == is_dir):
                        failed = True
                        raise OSError("injected_sync")
                    return real_fsync(fd)

                class PartialWriter:
                    def __init__(self, fd, mode, closefd):
                        self.stream = real_fdopen(fd, mode, closefd=closefd)

                    def __enter__(self):
                        self.stream.__enter__()
                        return self

                    def __exit__(self, *args):
                        return self.stream.__exit__(*args)

                    def write(self, data):
                        self.stream.write(data[:1])
                        raise OSError("injected_write")

                with (
                    mock.patch.object(
                        os,
                        "fsync",
                        side_effect=sync if phase != "write" else real_fsync,
                    ),
                    mock.patch.object(
                        os,
                        "fdopen",
                        side_effect=PartialWriter if phase == "write" else real_fdopen,
                    ),
                    self.assertRaisesRegex(OSError, "injected_"),
                ):
                    fault_probe.Probe._exclusive_write(
                        self.directory, "marker", b"proof"
                    )
                self.assertFalse((self.root / "marker").exists())
                fault_probe.Probe._exclusive_write(self.directory, "marker", b"proof")
                self.assertEqual((self.root / "marker").read_bytes(), b"proof\n")
                (self.root / "marker").unlink()

    def test_existing_or_replaced_file_is_never_removed(self):
        path = self.root / "marker"
        path.write_bytes(b"winner")
        before = path.stat()
        with self.assertRaises(FileExistsError):
            fault_probe.Probe._exclusive_write(self.directory, "marker", b"loser")
        self.assertEqual(path.read_bytes(), b"winner")
        self.assertEqual(path.stat().st_ino, before.st_ino)
        identity = (before.st_dev, before.st_ino)
        with path.open("rb"):
            path.unlink()
            path.write_bytes(b"replacement")
            after = path.stat()
            self.assertNotEqual((after.st_dev, after.st_ino), identity)
            fault_probe.Probe._remove_owned(self.directory, "marker", identity)
            self.assertEqual(path.read_bytes(), b"replacement")

    def test_cleanup_failure_does_not_replace_original_error(self):
        with (
            mock.patch.object(os, "fsync", side_effect=OSError("original_sync")),
            mock.patch.object(os, "unlink", side_effect=OSError("cleanup_failed")),
            self.assertRaisesRegex(OSError, "original_sync"),
        ):
            fault_probe.Probe._exclusive_write(self.directory, "marker", b"proof")
        self.assertTrue((self.root / "marker").exists())

    def test_two_processes_have_one_exclusive_winner(self):
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        processes = [
            context.Process(
                target=_compete_for_marker, args=(str(self.root), barrier, results)
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertCountEqual(
                [results.get(timeout=5), results.get(timeout=5)], ["created", "exists"]
            )
            self.assertEqual((self.root / "race-marker").read_bytes(), b"winner\n")
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()

    def test_two_processes_can_trigger_the_cut_only_once(self):
        arm_path = self.root / "arm.json"
        arm_path.write_text(
            json.dumps(
                {
                    "schema": "dlr.ingress-fault-arm/v1",
                    "arm_id": "test-cut",
                    "point": "target_after_commit_before_result",
                    "role": "worker",
                    "match": {"subscription_id": 11, "run_id": "fault-unit"},
                    "result_proof_mode": "reference-single-v1",
                }
            )
        )
        arm_path.chmod(0o600)
        context = multiprocessing.get_context("spawn")
        barrier, results = context.Barrier(2), context.Queue()
        processes = [
            context.Process(
                target=_compete_for_cut, args=(str(self.root), barrier, results)
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
            self.assertCountEqual([process.exitcode for process in processes], [0, 137])
            self.assertFalse(results.get(timeout=5))
            marker = json.loads((self.root / "fault-test-cut.json").read_bytes())
            receipt = (self.root / "fault-test-cut.result-evidence.json").read_bytes()
            self.assertEqual(
                marker["diagnostic_receipt_sha256"], hashlib.sha256(receipt).hexdigest()
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()

    def test_receipt_is_rolled_back_when_marker_sync_fails_and_can_retry(self):
        arm_path = self.root / "arm.json"
        arm_path.write_text(
            json.dumps(
                {
                    "schema": "dlr.ingress-fault-arm/v1",
                    "arm_id": "test-cut",
                    "point": "target_after_commit_before_result",
                    "role": "worker",
                    "match": {"subscription_id": 11, "run_id": "fault-unit"},
                    "result_proof_mode": "reference-single-v1",
                }
            )
        )
        arm_path.chmod(0o600)
        probe = fault_probe.Probe(arm_path, "worker")
        arm = probe.arm()
        self.assertIsNotNone(arm)
        ids = {"subscription_id": 11, "run_id": "fault-unit"}
        marker = self.root / "fault-test-cut.json"
        receipt = self.root / "fault-test-cut.result-evidence.json"
        real_fsync = os.fsync
        failed = False

        def fail_marker_sync(fd):
            nonlocal failed
            info = os.fstat(fd)
            if (
                not failed
                and marker.exists()
                and (info.st_dev, info.st_ino)
                == (marker.stat().st_dev, marker.stat().st_ino)
            ):
                failed = True
                raise OSError("marker_sync")
            return real_fsync(fd)

        env = {
            "DLR_INGRESS_FAULT_ENABLED": "1",
            "DLR_INGRESS_FAULT_SCOPE": "issue222-isolated",
            "DLR_INGRESS_FAULT_ROLE": "worker",
            "DLR_INGRESS_FAULT_ARM": str(arm_path),
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(
                os, "_exit", side_effect=RuntimeError("cut")
            ) as exit_call,
        ):
            with mock.patch.object(os, "fsync", side_effect=fail_marker_sync):
                self.assertFalse(probe.fire(arm, ids, {}, diagnostic_receipt=True))
            exit_call.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertFalse(receipt.exists())
            with self.assertRaisesRegex(RuntimeError, "cut"):
                probe.fire(arm, ids, {}, diagnostic_receipt=True)
            exit_call.assert_called_once_with(137)
        proof = json.loads(marker.read_bytes())
        self.assertEqual(
            proof["diagnostic_receipt_sha256"],
            hashlib.sha256(receipt.read_bytes()).hexdigest(),
        )

    def test_marker_winner_is_preserved_when_this_receipt_loses_the_race(self):
        arm_path = self.root / "arm.json"
        arm_path.write_text(
            json.dumps(
                {
                    "schema": "dlr.ingress-fault-arm/v1",
                    "arm_id": "test-cut",
                    "point": "target_after_commit_before_result",
                    "role": "worker",
                    "match": {"subscription_id": 11, "run_id": "fault-unit"},
                    "result_proof_mode": "reference-single-v1",
                }
            )
        )
        arm_path.chmod(0o600)
        probe = fault_probe.Probe(arm_path, "worker")
        arm = probe.arm()
        self.assertIsNotNone(arm)
        marker = self.root / "fault-test-cut.json"
        receipt = self.root / "fault-test-cut.result-evidence.json"
        original_write = probe._exclusive_write

        def competing_write(directory, name, encoded):
            if name == marker.name:
                marker.write_bytes(b"other-winner")
            return original_write(directory, name, encoded)

        env = {
            "DLR_INGRESS_FAULT_ENABLED": "1",
            "DLR_INGRESS_FAULT_SCOPE": "issue222-isolated",
            "DLR_INGRESS_FAULT_ROLE": "worker",
            "DLR_INGRESS_FAULT_ARM": str(arm_path),
        }
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(probe, "_exclusive_write", side_effect=competing_write),
            mock.patch.object(os, "_exit") as exit_call,
        ):
            self.assertFalse(
                probe.fire(
                    arm,
                    {"subscription_id": 11, "run_id": "fault-unit"},
                    {},
                    diagnostic_receipt=True,
                )
            )
            exit_call.assert_not_called()
        self.assertEqual(marker.read_bytes(), b"other-winner")
        self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
