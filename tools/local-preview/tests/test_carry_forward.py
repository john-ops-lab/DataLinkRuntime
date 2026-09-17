import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import carry_forward as carry


SHA_A, SHA_B = "a" * 40, "b" * 40


def table(columns, primary_key, rows=()):
    return {
        "columns": columns,
        "primary_key": primary_key,
        "rows": [dict(row) for row in rows],
    }


def tables(executions, attempts=(), incidents=(), slots=(), cleanup_requests=()):
    values = {
        name: table(["id"], ["id"], []) for name in carry.RESPONSIBILITY_TABLES
    }
    values.update(
        {
            "executions": table(list(executions[0]) if executions else ["id"], ["id"], executions),
            "execution_attempts": table(
                list(attempts[0]) if attempts else ["id", "execution_id", "status"],
                ["id"],
                attempts,
            ),
            "execution_infrastructure_incidents": table(
                list(incidents[0]) if incidents else ["id", "execution_id", "status"],
                ["id"],
                incidents,
            ),
            "adapter_execution_slots": table(
                list(slots[0]) if slots else ["id", "active_attempt_id"],
                ["id"],
                slots,
            ),
            "worker_cleanup_requests": table(
                list(cleanup_requests[0]) if cleanup_requests else ["id", "status"],
                ["id"],
                cleanup_requests,
            ),
        }
    )
    queued = [row for row in executions if row.get("status") == "queued"]
    if queued:
        admissions = [
            {
                "adapter_id": row["adapter_id"],
                "outstanding_count": 1,
                "outstanding_bytes": 0,
            }
            for row in queued
        ]
        values["adapter_execution_admission"] = table(
            list(admissions[0]), ["adapter_id"], admissions
        )
        values["global_execution_admission"] = table(
            ["singleton_key", "outstanding_count", "outstanding_bytes"],
            ["singleton_key"],
            [
                {
                    "singleton_key": "global",
                    "outstanding_count": len(queued),
                    "outstanding_bytes": 0,
                }
            ],
        )
        outbox = [
            {
                "id": row["id"],
                "execution_id": row["id"],
                "dispatch_generation": row["dispatch_generation"],
            }
            for row in queued
        ]
        values["execution_outbox"] = table(list(outbox[0]), ["id"], outbox)
    return values


def queued_execution(execution_id=7):
    return {
        "id": execution_id,
        "adapter_id": execution_id,
        "status": "queued",
        "dispatch_backend": "rabbitmq",
        "dispatch_generation": 1,
        "attempt_count": 0,
        "worker_id": None,
        "started_at": None,
        "workspace_cleanup_status": "pending",
        "admission_released_at": None,
        "claim_token_hash": None,
        "cleanup_receipt_token_hash": None,
    }


class SelectionTests(unittest.TestCase):
    def test_closed_selection_and_no_wildcards_or_duplicates(self):
        value = {
            "queued": [{"execution_id": 7, "incident_ids": [12, 11]}],
            "cleanup_execution_ids": [9],
        }
        self.assertEqual(
            carry.normalize_selection(value),
            {
                "queued": [{"execution_id": 7, "incident_ids": [11, 12]}],
                "cleanup_execution_ids": [9],
            },
        )
        for bad in (
            {"queued": [], "cleanup_execution_ids": []},
            {"queued": "*", "cleanup_execution_ids": []},
            {"queued": [{"execution_id": True, "incident_ids": [1]}], "cleanup_execution_ids": []},
            {
                "queued": [
                    {"execution_id": 7, "incident_ids": [11]},
                    {"execution_id": 7, "incident_ids": [12]},
                ],
                "cleanup_execution_ids": [],
            },
        ):
            with self.assertRaises(carry.CarryForwardError):
                carry.normalize_selection(bad)


class ResponsibilityTests(unittest.TestCase):
    def setUp(self):
        self.selection = {
            "queued": [{"execution_id": 7, "incident_ids": [11]}],
            "cleanup_execution_ids": [],
        }

    def test_never_claimed_pending_is_derived_without_mutation(self):
        execution = queued_execution()
        data = tables(
            [execution],
            incidents=[
                {
                    "id": 11,
                    "execution_id": 7,
                    "dispatch_generation": 1,
                    "status": "open",
                }
            ],
        )
        result = carry.derive_responsibilities(data, self.selection)
        self.assertEqual(result["executions"][0]["cleanup"], "not_applicable")
        self.assertEqual(execution["workspace_cleanup_status"], "pending")

    def test_terminal_deferred_requires_terminal_attempt(self):
        token = hashlib.sha256(b"cleanup").hexdigest()
        execution = dict(
            queued_execution(9),
            status="cancelled",
            attempt_count=1,
            worker_id=2,
            started_at="now",
            workspace_cleanup_status="deferred",
            cleanup_receipt_token_hash=token,
        )
        attempt = {
            "id": 13,
            "execution_id": 9,
            "attempt_no": 1,
            "fencing_token": 3,
            "status": "worker_lost",
        }
        result = carry.derive_responsibilities(
            tables([execution], attempts=[attempt]),
            {"queued": [], "cleanup_execution_ids": [9]},
        )
        self.assertEqual(result["executions"][0]["cleanup"], "deferred_preserved")

    def test_terminal_pending_without_attempt_is_not_applicable(self):
        execution = dict(queued_execution(9), status="cancelled")
        result = carry.derive_responsibilities(
            tables([execution]), {"queued": [], "cleanup_execution_ids": [9]}
        )
        self.assertEqual(result["executions"][0]["cleanup"], "not_applicable")
        self.assertEqual(execution["workspace_cleanup_status"], "pending")

    def test_active_slot_attempt_cleanup_and_unselected_busy_fail_closed(self):
        base = tables(
            [queued_execution()],
            incidents=[
                {
                    "id": 11,
                    "execution_id": 7,
                    "dispatch_generation": 1,
                    "status": "open",
                }
            ],
        )
        cases = [
            ("execution_attempts", [{"id": 1, "execution_id": 7, "status": "running"}]),
            ("adapter_execution_slots", [{"id": 1, "active_attempt_id": 1}]),
            ("worker_cleanup_requests", [{"id": 1, "status": "pending"}]),
        ]
        for name, rows in cases:
            changed = {key: dict(value) for key, value in base.items()}
            changed[name] = table(list(rows[0]), ["id"], rows)
            with self.assertRaises(carry.CarryForwardError):
                carry.derive_responsibilities(changed, self.selection)
        changed = tables(
            [queued_execution(), queued_execution(8)],
            incidents=[
                {
                    "id": 11,
                    "execution_id": 7,
                    "dispatch_generation": 1,
                    "status": "open",
                }
            ],
        )
        with self.assertRaisesRegex(carry.CarryForwardError, "unselected_execution_busy"):
            carry.derive_responsibilities(changed, self.selection)


class FileEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.journal = self.root / "journal"
        self.runtime.mkdir(mode=0o700)
        self.journal.mkdir(mode=0o700)

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_never_claimed_rejects_workspace(self):
        workspace = self.runtime / "workspaces/attempt-3/dlr-exec-7"
        workspace.mkdir(parents=True)
        evidence = carry.capture_files(self.runtime, self.journal)
        responsibilities = {
            "executions": [{"execution_id": 7, "attempt_ids": [], "cleanup": "not_applicable"}]
        }
        with self.assertRaisesRegex(carry.CarryForwardError, "never_claimed_storage_present"):
            carry.validate_file_responsibilities(evidence, responsibilities)

    def test_deferred_journal_identity_and_token_are_verified(self):
        token = "cleanup-token"
        self._write(
            self.journal / "execution-9-attempt-13.cleanup.json",
            {
                "cleanup_token": token,
                "execution_id": 9,
                "protocol_version": 3,
                "workspace_path": "/var/lib/dlr/runtime/workspaces/attempt-13/dlr-exec-9",
                "attempt_id": 13,
            },
        )
        evidence = carry.capture_files(self.runtime, self.journal)
        responsibilities = {
            "executions": [
                {
                    "execution_id": 9,
                    "attempt_ids": [13],
                    "cleanup": "deferred_preserved",
                    "cleanup_receipt_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                }
            ]
        }
        carry.validate_file_responsibilities(evidence, responsibilities)
        responsibilities["executions"][0]["cleanup_receipt_token_hash"] = "0" * 64
        with self.assertRaisesRegex(
            carry.CarryForwardError, "deferred_journal_identity_invalid"
        ):
            carry.validate_file_responsibilities(evidence, responsibilities)

    def test_deferred_without_journal_is_rejected(self):
        evidence = carry.capture_files(self.runtime, self.journal)
        with self.assertRaisesRegex(carry.CarryForwardError, "deferred_journal_missing"):
            carry.validate_file_responsibilities(
                evidence,
                {
                    "executions": [
                        {
                            "execution_id": 9,
                            "attempt_ids": [13],
                            "cleanup": "deferred_preserved",
                            "cleanup_receipt_token_hash": "0" * 64,
                        }
                    ]
                },
            )

    def test_symlinks_and_non_closed_journals_are_rejected(self):
        (self.runtime / "bad").symlink_to(self.journal)
        with self.assertRaisesRegex(carry.CarryForwardError, "storage_symlink_rejected"):
            carry.capture_files(self.runtime, self.journal)
        (self.runtime / "bad").unlink()
        self._write(
            self.journal / "execution-9-attempt-13.cleanup.json",
            {"execution_id": 9},
        )
        with self.assertRaisesRegex(carry.CarryForwardError, "cleanup_journal_invalid"):
            carry.capture_files(self.runtime, self.journal)


class ManifestAndProjectionTests(unittest.TestCase):
    def test_projection_is_ordered_and_exact(self):
        first = tables([queued_execution()])
        left = carry.project_rows(first)
        right = json.loads(json.dumps(left))
        carry.compare_projection(left, right)
        right["executions"]["rows"] = ["0" * 64]
        with self.assertRaisesRegex(carry.CarryForwardError, "projection_rows_changed"):
            carry.compare_projection(left, right)

    def test_manifest_digest_and_candidate_binding_are_closed(self):
        payload = {
            "format_version": 1,
            "manifest_id": "1" * 32,
            "created_at": "2026-09-17T00:00:00+00:00",
            "repo": "owner/repo",
            "pr": 2,
            "from_sha": SHA_A,
            "to_sha": SHA_B,
            "from_schema": "0031",
            "to_schema": "0032",
            "controller_files_digest": "c" * 64,
            "migration_graph_digest": "d" * 64,
            "old_image_ids": {},
            "candidate_image_ids": {},
            "selection": {
                "queued": [{"execution_id": 7, "incident_ids": [11]}],
                "cleanup_execution_ids": [],
            },
            "responsibilities": {},
            "old_runtime_projection": carry.project_rows(tables([queued_execution()])),
            "storage_identity": {},
            "file_evidence": {},
            "kernel_evidence": {},
        }
        manifest = carry.seal_manifest(payload)
        carry.validate_manifest(manifest)
        manifest["to_sha"] = SHA_A
        with self.assertRaisesRegex(carry.CarryForwardError, "manifest_digest_mismatch"):
            carry.validate_manifest(manifest)

    def test_private_file_requires_restricted_parent_and_regular_single_link(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o700)
            target = parent / "manifest.json"
            carry.write_private(target, {"safe": True})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(carry.read_private(target), {"safe": True})
            parent.chmod(0o755)
            with self.assertRaisesRegex(carry.CarryForwardError, "private_parent_invalid"):
                carry.read_private(target)

    def test_kernel_compare_rejects_unknown_or_repopulated_children(self):
        baseline = {
            "boot_id": "boot",
            "unit": "dlr-test.service",
            "control_group": "/system.slice/dlr-test.service",
            "keeper_pid": 7,
            "keeper_starttime": "10",
            "children": {"agent": 1, "old-worker": 1},
        }
        after = dict(baseline, children={"agent": 1})
        carry.compare_kernel(baseline, after)
        for children in ({"agent": 1, "unknown": 0}, {"agent": 1, "attempt-7-9": 1}):
            with self.assertRaisesRegex(carry.CarryForwardError, "kernel_not_idle"):
                carry.compare_kernel(baseline, dict(after, children=children))


if __name__ == "__main__":
    unittest.main()
