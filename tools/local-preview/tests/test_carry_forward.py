import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    attempts = [dict(row) for row in attempts]
    for row in attempts:
        if row.get("status") in carry.TERMINAL_ATTEMPTS:
            row.setdefault("cleanup_summary", {"workspace_cleanup_status": "completed"})
    values = {name: table(["id"], ["id"], []) for name in carry.RESPONSIBILITY_TABLES}
    values.update(
        {
            "executions": table(
                list(executions[0]) if executions else ["id"], ["id"], executions
            ),
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


def audited_case():
    from_outbox = uuid.UUID("00000000-0000-0000-0000-000000000101")
    to_outbox = uuid.UUID("00000000-0000-0000-0000-000000000102")
    disposition = "00000000-0000-0000-0000-000000000201"
    execution = {
        "id": 9,
        "adapter_id": 9,
        "status": "succeeded",
        "dispatch_backend": "rabbitmq",
        "dispatch_generation": 2,
        "attempt_count": 1,
        "worker_id": 3,
        "started_at": "2026-09-19T00:00:00+00:00",
        "ended_at": "2026-09-19T00:00:01+00:00",
        "workspace_cleanup_status": "completed",
        "admission_released_at": "2026-09-19T00:00:02+00:00",
        "logical_input_bytes": 4,
        "output": {"ok": True},
        "error_code": None,
        "last_error_code": None,
        "replay_of_execution_id": None,
    }
    intent = {
        "action": "recover",
        "expected_generation": 1,
        "reason_code": "capacity_repaired",
    }
    audit = {
        "id": uuid.UUID(disposition),
        "incident_id": 19,
        "execution_id": 9,
        "idempotency_key": uuid.UUID("00000000-0000-0000-0000-000000000301"),
        "request_hash": hashlib.sha256(carry.canonical_bytes(intent)).hexdigest(),
        "actor_kind": "superadmin",
        "user_id": None,
        "action": "recover",
        "reason_code": "capacity_repaired",
        "outcome": "recovery_dispatched",
        "code": "recovery_dispatched",
        "from_generation": 1,
        "to_generation": 2,
        "from_outbox_id": from_outbox,
        "to_outbox_id": to_outbox,
        "execution_status": "queued",
        "created_at": "2026-09-19T00:00:00+00:00",
    }
    data = tables(
        [execution],
        attempts=[
            {
                "id": 29,
                "execution_id": 9,
                "attempt_no": 1,
                "fencing_token": 8,
                "status": "succeeded",
                "ended_at": "2026-09-19T00:00:01+00:00",
                "error_code": None,
            }
        ],
        incidents=[
            {
                "id": 19,
                "execution_id": 9,
                "dispatch_generation": 1,
                "message_id": uuid.UUID("00000000-0000-0000-0000-000000000401"),
                "status": "resolved",
                "resolved_at": "2026-09-19T00:00:00+00:00",
            }
        ],
    )
    data["execution_outbox"] = table(
        [
            "id",
            "execution_id",
            "dispatch_generation",
            "message_id",
            "status",
            "last_error_code",
        ],
        ["id"],
        [
            {
                "id": from_outbox,
                "execution_id": 9,
                "dispatch_generation": 1,
                "message_id": uuid.UUID("00000000-0000-0000-0000-000000000401"),
                "status": "published",
                "last_error_code": None,
            },
            {
                "id": to_outbox,
                "execution_id": 9,
                "dispatch_generation": 2,
                "message_id": uuid.UUID("00000000-0000-0000-0000-000000000402"),
                "status": "published",
                "last_error_code": None,
            },
        ],
    )
    data[carry.AUDIT_TABLE] = table(list(carry.AUDIT_COLUMNS), ["id"], [audit])
    data["adapter_execution_admission"] = table(
        ["adapter_id", "outstanding_count", "outstanding_bytes"], ["adapter_id"], []
    )
    data["global_execution_admission"] = table(
        ["singleton_key", "outstanding_count", "outstanding_bytes"],
        ["singleton_key"],
        [{"singleton_key": "global", "outstanding_count": 0, "outstanding_bytes": 0}],
    )
    selection = {
        "queued": [],
        "cleanup_execution_ids": [],
        "terminal_executions": [
            {
                "execution_id": 9,
                "incident_id": 19,
                "disposition_id": disposition,
                "expected_status": "succeeded",
                "expected_generation": 2,
                "expected_output_digest": carry.digest({"ok": True}),
                "expected_error_code": None,
                "expected_last_error_code": None,
                "expected_attempt_count": 1,
            }
        ],
    }
    return data, selection


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
            {
                "queued": [{"execution_id": True, "incident_ids": [1]}],
                "cleanup_execution_ids": [],
            },
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
            "cleanup_summary": {"workspace_cleanup_status": "deferred"},
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
        with self.assertRaisesRegex(
            carry.CarryForwardError, "unselected_execution_busy"
        ):
            carry.derive_responsibilities(changed, self.selection)

    def test_unselected_terminal_deferred_is_not_ignored(self):
        selected = queued_execution()
        deferred = dict(
            queued_execution(9),
            status="cancelled",
            attempt_count=1,
            worker_id=2,
            started_at="now",
            workspace_cleanup_status="deferred",
        )
        with self.assertRaisesRegex(
            carry.CarryForwardError, "unselected_cleanup_responsibility"
        ):
            carry.derive_responsibilities(
                tables(
                    [selected, deferred],
                    attempts=[
                        {
                            "id": 13,
                            "execution_id": 9,
                            "attempt_no": 1,
                            "fencing_token": 2,
                            "status": "worker_lost",
                        }
                    ],
                    incidents=[
                        {
                            "id": 11,
                            "execution_id": 7,
                            "dispatch_generation": 1,
                            "status": "open",
                        }
                    ],
                ),
                self.selection,
            )

    def test_completed_execution_retains_historical_deferred_attempt(self):
        execution = dict(
            queued_execution(9),
            status="succeeded",
            attempt_count=2,
            worker_id=2,
            started_at="now",
            workspace_cleanup_status="completed",
        )
        attempts = [
            {
                "id": 13,
                "execution_id": 9,
                "attempt_no": 1,
                "fencing_token": 3,
                "status": "worker_lost",
                "cleanup_summary": {"workspace_cleanup_status": "deferred"},
            },
            {
                "id": 14,
                "execution_id": 9,
                "attempt_no": 2,
                "fencing_token": 4,
                "status": "succeeded",
                "cleanup_summary": {"workspace_cleanup_status": "completed"},
            },
        ]
        result = carry.derive_responsibilities(
            tables([execution], attempts=attempts),
            {"queued": [], "cleanup_execution_ids": [9]},
        )["executions"][0]
        self.assertEqual(result["cleanup"], "deferred_preserved")
        self.assertEqual(result["deferred_attempt_ids"], [13])


class AuditedResponsibilityTests(unittest.TestCase):
    def test_full_audit_terminal_relationship_is_accepted(self):
        data, selection = audited_case()
        result = carry.derive_terminal_evidence(data, selection)
        self.assertEqual(result["executions"][0]["execution_id"], 9)

    def test_terminal_cleanup_is_classified_even_when_not_selected_for_cleanup(self):
        cases = (
            ("execution unknown", "unknown", {"workspace_cleanup_status": "completed"}),
            ("execution null", None, {"workspace_cleanup_status": "completed"}),
            ("attempt null", "completed", None),
            ("attempt pending", "completed", {"workspace_cleanup_status": "pending"}),
        )
        for label, execution_cleanup, attempt_cleanup in cases:
            with self.subTest(label=label):
                data, selection = audited_case()
                data["executions"]["rows"][0][
                    "workspace_cleanup_status"
                ] = execution_cleanup
                data["execution_attempts"]["rows"][0][
                    "cleanup_summary"
                ] = attempt_cleanup
                with self.assertRaisesRegex(
                    carry.CarryForwardError,
                    "cleanup_state_unknown|attempt_cleanup_state_unknown",
                ):
                    carry.derive_terminal_evidence(data, selection)

    def test_hidden_audit_change_and_new_or_replaced_row_are_rejected(self):
        data, selection = audited_case()
        data[carry.AUDIT_TABLE]["rows"][0]["request_hash"] = "0" * 64
        with self.assertRaisesRegex(
            carry.CarryForwardError, "audit_request_hash_invalid"
        ):
            carry.derive_terminal_evidence(data, selection)

        data, selection = audited_case()
        replacement = dict(data[carry.AUDIT_TABLE]["rows"][0])
        replacement["id"] = uuid.UUID("00000000-0000-0000-0000-000000000999")
        data[carry.AUDIT_TABLE]["rows"] = [replacement]
        with self.assertRaisesRegex(carry.CarryForwardError, "audit_set_mismatch"):
            carry.derive_terminal_evidence(data, selection)

        data, selection = audited_case()
        data[carry.AUDIT_TABLE]["rows"].append(dict(data[carry.AUDIT_TABLE]["rows"][0]))
        with self.assertRaisesRegex(
            carry.CarryForwardError, "terminal_identity_duplicate"
        ):
            carry.derive_terminal_evidence(data, selection)

    def test_terminal_omission_and_admission_leak_are_rejected(self):
        data, selection = audited_case()
        selection["terminal_executions"] = []
        with self.assertRaisesRegex(carry.CarryForwardError, "selection_invalid"):
            carry.derive_terminal_evidence(data, selection)

    def test_legacy_terminal_is_not_a_charge_and_cannot_be_selected(self):
        data, selection = audited_case()
        legacy = dict(data["executions"]["rows"][0])
        legacy.update(
            id=50,
            adapter_id=50,
            dispatch_backend="legacy",
            admission_released_at=None,
            replay_of_execution_id=None,
        )
        data["executions"]["rows"].append(legacy)
        carry.derive_terminal_evidence(data, selection)
        selected_legacy = copy.deepcopy(selection)
        selected_legacy["terminal_executions"][0]["execution_id"] = 50
        with self.assertRaisesRegex(
            carry.CarryForwardError, "terminal_execution_mismatch"
        ):
            carry.derive_terminal_evidence(data, selected_legacy)

        data, selection = audited_case()
        data["global_execution_admission"]["rows"][0]["outstanding_count"] = 1
        with self.assertRaisesRegex(
            carry.CarryForwardError, "global_admission_mismatch"
        ):
            carry.derive_terminal_evidence(data, selection)

    def test_terminal_cleanup_overlap_is_allowed_but_queued_overlap_is_not(self):
        data, selection = audited_case()
        selection["cleanup_execution_ids"] = [9]
        carry.normalize_selection(selection, mode=carry.AUDITED_MODE)
        self.assertEqual(
            carry.derive_terminal_evidence(data, selection)["executions"][0][
                "execution_id"
            ],
            9,
        )
        selection["queued"] = [{"execution_id": 9, "incident_ids": [20]}]
        with self.assertRaisesRegex(carry.CarryForwardError, "selection_duplicate"):
            carry.normalize_selection(selection, mode=carry.AUDITED_MODE)

    def test_cancelled_zero_attempt_keeps_published_outbox_null_error(self):
        data, selection = audited_case()
        execution = data["executions"]["rows"][0]
        execution.update(
            status="cancelled",
            dispatch_generation=1,
            attempt_count=0,
            worker_id=None,
            started_at=None,
            workspace_cleanup_status="pending",
            output=None,
            error_code="execution_cancelled",
            last_error_code="execution_cancelled",
        )
        data["execution_attempts"]["rows"] = []
        data["execution_outbox"]["rows"] = data["execution_outbox"]["rows"][:1]
        audit = data[carry.AUDIT_TABLE]["rows"][0]
        audit.update(
            action="terminate",
            reason_code="operator_cancel",
            outcome="execution_terminal",
            code="execution_cancelled",
            to_generation=1,
            to_outbox_id=audit["from_outbox_id"],
            execution_status="cancelled",
        )
        audit["request_hash"] = hashlib.sha256(
            carry.canonical_bytes(
                {
                    "action": "terminate",
                    "expected_generation": 1,
                    "reason_code": "operator_cancel",
                }
            )
        ).hexdigest()
        expected = selection["terminal_executions"][0]
        expected.update(
            expected_status="cancelled",
            expected_generation=1,
            expected_output_digest=carry.digest(None),
            expected_error_code="execution_cancelled",
            expected_last_error_code="execution_cancelled",
            expected_attempt_count=0,
        )
        selection["cleanup_execution_ids"] = [9]
        self.assertIsNone(data["execution_outbox"]["rows"][0]["last_error_code"])
        carry.derive_terminal_evidence(data, selection)
        responsibilities = carry.derive_responsibilities(
            data, selection, mode=carry.AUDITED_MODE
        )
        self.assertEqual(responsibilities["executions"][0]["cleanup"], "not_applicable")

    def test_audit_projection_requires_all_actual_columns_and_primary_key(self):
        data, _ = audited_case()
        projection = carry.project_rows(data, required=carry.AUDITED_TABLES)
        carry.validate_projection_evidence(projection, required=carry.AUDITED_TABLES)
        changed = json.loads(json.dumps(projection))
        changed[carry.AUDIT_TABLE]["columns"].remove("request_hash")
        with self.assertRaisesRegex(carry.CarryForwardError, "audit_schema_invalid"):
            carry.validate_projection_evidence(changed, required=carry.AUDITED_TABLES)
        changed = json.loads(json.dumps(projection))
        changed[carry.AUDIT_TABLE]["primary_key"] = ["incident_id"]
        with self.assertRaisesRegex(carry.CarryForwardError, "audit_schema_invalid"):
            carry.validate_projection_evidence(changed, required=carry.AUDITED_TABLES)

    def test_v3_requires_exact_columns_for_all_fourteen_tables(self):
        baseline = {"executions": {"columns": ["id", "status"]}}
        self.assertEqual(
            carry.projection_columns(
                "executions",
                ["id", "status"],
                baseline,
                mode=carry.AUDITED_MODE,
            ),
            ["id", "status"],
        )
        for changed in (["id", "status", "new_column"], ["status", "id"], ["id"]):
            with self.subTest(columns=changed):
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "projection_columns_changed"
                ):
                    carry.projection_columns(
                        "executions", changed, baseline, mode=carry.AUDITED_MODE
                    )
        self.assertEqual(
            carry.projection_columns(
                "executions", ["id", "status", "forward_addition"], baseline, mode=None
            ),
            ["id", "status"],
        )

    def test_same_count_audit_row_change_fails_projection_comparison(self):
        data, _ = audited_case()
        before = carry.project_rows(data, required=carry.AUDITED_TABLES)
        data[carry.AUDIT_TABLE]["rows"][0]["idempotency_key"] = uuid.UUID(
            "00000000-0000-0000-0000-000000000777"
        )
        after = carry.project_rows(data, required=carry.AUDITED_TABLES)
        with self.assertRaisesRegex(carry.CarryForwardError, "projection_rows_changed"):
            carry.compare_projection(before, after)

    def test_manifest_v3_is_explicit_and_v2_shape_stays_closed(self):
        data, selection = audited_case()
        terminal = carry.derive_terminal_evidence(data, selection)["executions"]
        payload = {
            "format_version": carry.AUDITED_FORMAT_VERSION,
            "mode": carry.AUDITED_MODE,
            "source_diff": {
                "tree_digest": "9" * 64,
                "entries": [
                    {
                        "status": "M",
                        "old_mode": "100644",
                        "new_mode": "100644",
                        "old_oid": "a" * 40,
                        "new_oid": "b" * 40,
                        "path": "web/src/index.css",
                    }
                ],
            },
            "manifest_id": "1" * 32,
            "created_at": "2026-09-19T00:00:00+00:00",
            "repo": "owner/repo",
            "pr": 2,
            "from_sha": SHA_A,
            "to_sha": SHA_B,
            "from_schema": "0040_issue152_dispositions",
            "to_schema": "0040_issue152_dispositions",
            "controller_files_digest": "c" * 64,
            "migration_graph_digest": "d" * 64,
            "old_image_ids": {},
            "candidate_image_ids": {},
            "selection": selection,
            "responsibilities": {"executions": [], "terminal_executions": terminal},
            "old_runtime_projection": carry.project_rows(
                data, required=carry.AUDITED_TABLES
            ),
            "schema_inventory": {"tables": sorted(carry.AUDITED_TABLES)},
            "storage_identity": [],
            "old_containers": [],
            "file_evidence": {},
            "kernel_evidence": {},
        }
        carry.validate_manifest(carry.seal_manifest(payload))
        with self.assertRaisesRegex(
            carry.CarryForwardError, "manifest_version_invalid"
        ):
            carry.seal_manifest(dict(payload, format_version=3.0))
        for invalid_version in (True, "3"):
            with self.assertRaises(carry.CarryForwardError):
                carry.seal_manifest(dict(payload, format_version=invalid_version))
        mixed = dict(payload, format_version=carry.FORMAT_VERSION)
        mixed["manifest_digest"] = "0" * 64
        with self.assertRaisesRegex(carry.CarryForwardError, "manifest_shape_invalid"):
            carry.validate_manifest(mixed)


class FileEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.journal = self.root / "journal"
        self.runtime.mkdir(mode=0o711)
        self.journal.mkdir(mode=0o700)
        for root in (self.runtime, self.journal):
            lock = root / ".dlr-instance.lock"
            lock.touch(mode=0o600)
        attempt_journal = self.runtime / "attempt-journal"
        attempt_journal.mkdir(mode=0o700)
        (attempt_journal / ".dlr-instance.lock").touch(mode=0o600)
        (self.runtime / "workspaces").mkdir(mode=0o700)
        cache = self.runtime / "version-cache"
        cache.mkdir(mode=0o711)
        (cache / "entries").mkdir(mode=0o711)
        (cache / ".dlr-cache-reservations.json").write_text("{}")
        (cache / ".dlr-cache-reservations.json").chmod(0o600)
        (cache / ".dlr-cache-reservations.lock").touch(mode=0o644)
        (self.journal / "sandbox-recovery").mkdir(mode=0o700)

    def _write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_never_claimed_rejects_workspace(self):
        workspace = self.runtime / "workspaces/attempt-3/dlr-exec-7"
        workspace.mkdir(parents=True)
        workspace.parent.chmod(0o700)
        workspace.chmod(0o700)
        evidence = carry.capture_files(self.runtime, self.journal)
        responsibilities = {
            "executions": [
                {"execution_id": 7, "attempt_ids": [], "cleanup": "not_applicable"}
            ]
        }
        with self.assertRaisesRegex(carry.CarryForwardError, "workspace_entry_unknown"):
            carry.validate_file_responsibilities(evidence, responsibilities)

    def test_file_metadata_changes_preservation_fingerprint(self):
        path = self.runtime / ".dlr-instance.lock"
        path.write_bytes(b"")
        before = carry.capture_files(self.runtime, self.journal)
        changed = path.stat().st_mtime_ns + 10_000_000_000
        os.utime(path, ns=(changed, changed))
        after = carry.capture_files(self.runtime, self.journal)
        self.assertNotEqual(before, after)

    def test_real_layout_contract_allows_cache_and_empty_attempt_shells(self):
        cached_bin = self.runtime / "version-cache/entries/a-v/.venv/bin"
        cached_bin.mkdir(parents=True)
        (cached_bin / "python").symlink_to("/usr/bin/python3")
        for attempt_id in range(1, 462):
            (self.runtime / f"workspaces/attempt-{attempt_id}").mkdir(mode=0o700)
        evidence = carry.capture_files(
            self.runtime,
            self.journal,
            attempt_statuses={attempt_id: "succeeded" for attempt_id in range(1, 450)},
        )
        carry.validate_file_responsibilities(
            evidence,
            {
                "executions": [
                    {
                        "execution_id": 900,
                        "attempt_ids": [],
                        "cleanup": "not_applicable",
                    }
                ]
            },
        )
        self.assertIn(
            "version-cache/entries",
            {entry["path"] for entry in evidence["runtime"]["entries"]},
        )
        classifications = {
            item["attempt_id"]: item["classification"]
            for item in evidence["empty_attempt_shells"]
        }
        self.assertEqual(classifications[1], "terminal_attempt_empty_shell")
        self.assertEqual(classifications[461], "owned_empty_shell_without_db_row")

    def test_trusted_roots_and_lock_types_are_closed(self):
        self.runtime.chmod(0o777)
        with self.assertRaisesRegex(carry.CarryForwardError, "runtime_root_invalid"):
            carry.capture_files(self.runtime, self.journal)
        self.runtime.chmod(0o711)
        lock = self.runtime / ".dlr-instance.lock"
        lock.unlink()
        lock.mkdir(mode=0o700)
        with self.assertRaisesRegex(carry.CarryForwardError, "storage_lock_invalid"):
            carry.capture_files(self.runtime, self.journal)

    def test_unknown_workspace_shapes_are_rejected(self):
        unknown = self.runtime / "workspaces/unknown-owner"
        unknown.mkdir()
        with self.assertRaisesRegex(carry.CarryForwardError, "workspace_entry_unknown"):
            carry.validate_file_responsibilities(
                carry.capture_files(self.runtime, self.journal), {"executions": []}
            )

    def test_individual_credential_hashes_are_not_persisted(self):
        token = "cleanup-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
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
        evidence = carry.capture_files(
            self.runtime,
            self.journal,
            credential_hashes={
                13: {"claim_token_hash": None, "cleanup_token_hash": token_hash}
            },
        )
        self.assertTrue(
            evidence["journal_facts"]["cleanup"][0]["cleanup_token_matches"]
        )
        self.assertNotIn(token_hash, json.dumps(evidence, sort_keys=True))

    def test_deferred_journal_identity_and_token_are_verified(self):
        token = "cleanup-token"
        attempt_root = self.runtime / "workspaces/attempt-13"
        attempt_root.mkdir(mode=0o700)
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
        self._write(
            self.journal / "sandbox-recovery/sandbox-attempt-9-13.json",
            {
                "cgroup_name": "attempt-9-13",
                "execution_id": 9,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(attempt_root / ".dlr-sandbox-mount"),
                "namespace_identity": {
                    "boot_id": "11111111-1111-1111-1111-111111111111",
                    "parent_device": 10,
                    "parent_inode": 20,
                    "root_device": 10,
                    "root_inode": 30,
                },
                "cgroup_device": 10,
                "cgroup_inode": 30,
            },
        )
        evidence = carry.capture_files(
            self.runtime,
            self.journal,
            credential_hashes={
                13: {
                    "cleanup_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                    "claim_token_hash": None,
                }
            },
        )
        responsibilities = {
            "executions": [
                {
                    "execution_id": 9,
                    "attempt_ids": [13],
                    "deferred_attempt_ids": [13],
                    "attempts": [
                        {
                            "attempt_id": 13,
                            "cleanup_token_hash": hashlib.sha256(
                                token.encode()
                            ).hexdigest(),
                        }
                    ],
                    "cleanup": "deferred_preserved",
                }
            ]
        }
        carry.validate_file_responsibilities(evidence, responsibilities)
        self.assertEqual(
            evidence["empty_attempt_shells"],
            [
                {
                    "attempt_id": 13,
                    "classification": "deferred_responsibility_empty_shell",
                }
            ],
        )
        carry.validate_retired_markers(
            evidence["journal_facts"]["sandbox_recovery"],
            boot_id="11111111-1111-1111-1111-111111111111",
            parent_device=10,
            parent_inode=20,
            children={"agent": {"device": 10, "inode": 21}},
        )
        unowned = json.loads(json.dumps(responsibilities))
        unowned["executions"][0]["deferred_attempt_ids"] = []
        with self.assertRaisesRegex(
            carry.CarryForwardError, "workspace_identity_invalid"
        ):
            carry.validate_file_responsibilities(evidence, unowned)
        evidence["journal_facts"]["cleanup"][0]["cleanup_token_matches"] = False
        with self.assertRaisesRegex(
            carry.CarryForwardError, "deferred_journal_identity_invalid"
        ):
            carry.validate_file_responsibilities(evidence, responsibilities)

    def test_deferred_without_journal_is_rejected(self):
        evidence = carry.capture_files(self.runtime, self.journal)
        with self.assertRaisesRegex(
            carry.CarryForwardError, "deferred_journal_missing"
        ):
            carry.validate_file_responsibilities(
                evidence,
                {
                    "executions": [
                        {
                            "execution_id": 9,
                            "attempt_ids": [13],
                            "deferred_attempt_ids": [13],
                            "attempts": [
                                {"attempt_id": 13, "cleanup_token_hash": "0" * 64}
                            ],
                            "cleanup": "deferred_preserved",
                        }
                    ]
                },
            )

    def test_symlinks_and_non_closed_journals_are_rejected(self):
        (self.runtime / "bad").symlink_to(self.journal)
        with self.assertRaisesRegex(carry.CarryForwardError, "storage_entry_unknown"):
            carry.capture_files(self.runtime, self.journal)
        (self.runtime / "bad").unlink()
        self._write(
            self.journal / "execution-9-attempt-13.cleanup.json",
            {"execution_id": 9},
        )
        with self.assertRaisesRegex(carry.CarryForwardError, "cleanup_journal_invalid"):
            carry.capture_files(self.runtime, self.journal)

    def test_unknown_cleanup_file_is_rejected_by_responsibility_check(self):
        self._write(self.journal / "unknown.cleanup.json", {"unknown": True})
        evidence = carry.capture_files(self.runtime, self.journal)
        with self.assertRaisesRegex(carry.CarryForwardError, "cleanup_journal_unknown"):
            carry.validate_file_responsibilities(
                evidence,
                {
                    "executions": [
                        {
                            "execution_id": 7,
                            "attempt_ids": [],
                            "cleanup": "not_applicable",
                        }
                    ]
                },
            )

    def test_attempt_journal_must_match_attempt_fence_and_tokens(self):
        claim, cleanup = "claim-token", "cleanup-token"
        self._write(
            self.runtime / "attempt-journal/attempt-13.attempt.json",
            {
                "attempt_id": 13,
                "attempt_no": 2,
                "claim_token": claim,
                "cleanup_token": cleanup,
                "execution_id": 9,
                "fencing_token": 4,
                "lease_expires_at": "2026-09-17T01:00:00+00:00",
                "protocol_version": 3,
                "workspace_path": "/var/lib/dlr/runtime/workspaces/attempt-13/dlr-exec-9",
            },
        )
        responsibilities = {
            "executions": [
                {
                    "execution_id": 9,
                    "attempt_ids": [13],
                    "attempts": [
                        {
                            "attempt_id": 13,
                            "attempt_no": 2,
                            "fencing_token": 4,
                            "claim_token_hash": hashlib.sha256(
                                claim.encode()
                            ).hexdigest(),
                            "cleanup_token_hash": hashlib.sha256(
                                cleanup.encode()
                            ).hexdigest(),
                            "lease_expires_at": "2026-09-17T01:00:00+00:00",
                        }
                    ],
                    "cleanup": "completed",
                }
            ]
        }
        evidence = carry.capture_files(
            self.runtime,
            self.journal,
            credential_hashes={
                13: {
                    "claim_token_hash": hashlib.sha256(claim.encode()).hexdigest(),
                    "cleanup_token_hash": hashlib.sha256(cleanup.encode()).hexdigest(),
                }
            },
        )
        with self.assertRaisesRegex(
            carry.CarryForwardError, "completed_storage_present"
        ):
            carry.validate_file_responsibilities(evidence, responsibilities)
        responsibilities["executions"][0]["cleanup"] = "deferred_preserved"
        responsibilities["executions"][0]["deferred_attempt_ids"] = [13]
        self._write(
            self.journal / "execution-9-attempt-13.cleanup.json",
            {
                "cleanup_token": cleanup,
                "execution_id": 9,
                "protocol_version": 3,
                "workspace_path": "/var/lib/dlr/runtime/workspaces/attempt-13/dlr-exec-9",
                "attempt_id": 13,
            },
        )
        evidence = carry.capture_files(
            self.runtime,
            self.journal,
            credential_hashes={
                13: {
                    "claim_token_hash": hashlib.sha256(claim.encode()).hexdigest(),
                    "cleanup_token_hash": hashlib.sha256(cleanup.encode()).hexdigest(),
                }
            },
        )
        carry.validate_file_responsibilities(evidence, responsibilities)
        responsibilities["executions"][0]["attempts"][0]["fencing_token"] = 5
        with self.assertRaisesRegex(
            carry.CarryForwardError, "attempt_journal_identity_invalid"
        ):
            carry.validate_file_responsibilities(evidence, responsibilities)


class ManifestAndProjectionTests(unittest.TestCase):
    def test_selected_mount_uses_component_bounded_volume_root(self):
        targets = {("ext4", "8:1", "/docker/volumes/selected/_data")}
        base = {"filesystem": "ext4", "major_minor": "8:1"}
        self.assertTrue(
            carry._selected_mount_matches(
                dict(base, root="/docker/volumes/selected/_data/attempt-7"),
                targets,
            )
        )
        self.assertFalse(
            carry._selected_mount_matches(
                dict(base, root="/docker/volumes/selected/_data-other"),
                targets,
            )
        )
        self.assertFalse(
            carry._selected_mount_matches(
                dict(base, root="/docker/volumes/other/_data"), targets
            )
        )

    def test_storage_shadow_runtime_config_and_cgroup_root_are_closed(self):
        storage = [
            {
                "service": "worker",
                "type": "volume",
                "source": "runtime",
                "destination": "/var/lib/dlr/runtime",
                "read_only": False,
            }
        ]
        carry.validate_storage_identity(storage)
        shadow = dict(
            storage[0],
            type="bind",
            source="/host/shadow",
            destination="/var/lib/dlr/runtime/workspaces",
        )
        with self.assertRaisesRegex(
            carry.CarryForwardError, "storage_identity_shadowed"
        ):
            carry.validate_storage_identity([*storage, shadow])
        config = carry._worker_runtime_config(
            {
                "User": "1000:1000",
                "Env": [
                    "DLR_RUNTIME_ROOT=/var/lib/dlr/runtime",
                    "DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT=/var/lib/dlr/journal",
                    "DLR_SANDBOX_CGROUP_PATH=/run/dlr-cgroup",
                ],
            }
        )
        self.assertEqual(
            config["attempt_journal_root"],
            "/var/lib/dlr/runtime/attempt-journal",
        )
        self.assertEqual(
            carry._delegated_worker_root(
                "0::/system.slice/dlr.service/docker-worker/agent",
                "/system.slice/dlr.service",
            ),
            Path("/sys/fs/cgroup/system.slice/dlr.service/docker-worker"),
        )
        with self.assertRaisesRegex(carry.CarryForwardError, "kernel_identity_unknown"):
            carry._delegated_worker_root(
                "0::/system.slice/dlr.service/a/b/agent",
                "/system.slice/dlr.service",
            )

    def test_namespace_census_finds_tid_and_pins_and_rejects_unknown_pin(self):
        authority = {
            "mount_namespace": "mnt:[4242]",
            "cgroup_namespace": "cgroup:[4243]",
            "volumes": {
                "runtime": {
                    "mount": {
                        "filesystem": "ext4",
                        "major_minor": "8:1",
                        "root": "/docker/volumes/selected-runtime/_data",
                    }
                }
            },
        }

        def process(root, namespace, mountinfo, fd_target=None):
            (root / "ns").mkdir(parents=True)
            (root / "ns/mnt").symlink_to(namespace)
            (root / "ns/cgroup").symlink_to("cgroup:[20]")
            (root / "mountinfo").write_text(mountinfo)
            (root / "fd").mkdir()
            if fd_target:
                (root / "fd/8").symlink_to(fd_target)

        def census(*, tid_mount="", leader_mount="", fd_target=None):
            with tempfile.TemporaryDirectory() as directory:
                fake_proc = Path(directory) / "proc"
                leader = fake_proc / "20"
                process(
                    leader,
                    "mnt:[20]",
                    leader_mount or "1 0 0:1 / / rw - proc proc rw\n",
                    fd_target,
                )
                if tid_mount:
                    process(leader / "task/21", "mnt:[21]", tid_mount)
                real_path = Path

                def mapped_path(value):
                    path = real_path(value)
                    if path == real_path("/proc"):
                        return fake_proc
                    return path

                with mock.patch.object(carry, "Path", side_effect=mapped_path):
                    return carry._related_mount_namespaces(authority)

        selected = (
            "1 0 8:1 /docker/volumes/selected-runtime/_data/attempt-7 "
            "/runtime rw - ext4 /dev/sda rw\n"
        )
        self.assertEqual(census(tid_mount=selected)["related_count"], 1)
        self.assertEqual(census(fd_target="mnt:[4242]")["pin_count"], 1)
        nsfs = "1 0 0:4 mnt:[4242] /pin rw - nsfs nsfs rw\n"
        self.assertEqual(census(leader_mount=nsfs)["pin_count"], 1)
        with self.assertRaisesRegex(carry.CarryForwardError, "kernel_identity_unknown"):
            census(fd_target="mnt:[999999]")

    def test_retired_marker_matches_worker_namespace_rules(self):
        marker = {
            "cgroup_name": "attempt-9-13",
            "marker_fingerprint": "f" * 64,
            "namespace_identity": {
                "boot_id": "11111111-1111-1111-1111-111111111111",
                "parent_device": 10,
                "parent_inode": 20,
                "root_device": 10,
                "root_inode": 30,
            },
        }
        result = carry.validate_retired_markers(
            [marker],
            boot_id="11111111-1111-1111-1111-111111111111",
            parent_device=10,
            parent_inode=20,
            children={"agent": {"device": 10, "inode": 21}},
        )
        self.assertEqual(result[0]["cgroup_name"], "attempt-9-13")
        cross_boot = json.loads(json.dumps(marker))
        cross_boot["namespace_identity"]["boot_id"] = (
            "22222222-2222-2222-2222-222222222222"
        )
        carry.validate_retired_markers(
            [cross_boot],
            boot_id="11111111-1111-1111-1111-111111111111",
            parent_device=99,
            parent_inode=99,
            children={"agent": {"device": 10, "inode": 21}},
        )
        for changed, children in (
            ({"parent_inode": 99}, {"agent": {"device": 10, "inode": 21}}),
            (
                {},
                {
                    "agent": {"device": 10, "inode": 21},
                    "old": {"device": 10, "inode": 30},
                },
            ),
        ):
            invalid = json.loads(json.dumps(marker))
            invalid["namespace_identity"].update(changed)
            with self.assertRaisesRegex(
                carry.CarryForwardError, "sandbox_namespace_not_retired"
            ):
                carry.validate_retired_markers(
                    [invalid],
                    boot_id="11111111-1111-1111-1111-111111111111",
                    parent_device=10,
                    parent_inode=20,
                    children=children,
                )

    def test_candidate_disposition_table_must_exist_and_be_empty(self):
        revision = "0040_issue152_dispositions"
        table_name = "execution_incident_dispositions"
        carry.validate_candidate_tables(revision, {table_name}, {table_name: 0})
        with self.assertRaisesRegex(carry.CarryForwardError, "candidate_table_missing"):
            carry.validate_candidate_tables(revision, set(), {})
        with self.assertRaisesRegex(
            carry.CarryForwardError, "candidate_table_not_empty"
        ):
            carry.validate_candidate_tables(revision, {table_name}, {table_name: 1})

    def test_schema_inventory_supports_forward_and_bounded_0040_successor(self):
        baseline = {"executions", "execution_attempts"}
        expected = baseline | {
            "runtime_reconciliation_cursors",
            "execution_incident_dispositions",
        }
        carry.validate_schema_inventory(
            "0038_issue138_languages",
            "0040_issue152_dispositions",
            baseline,
            expected,
            {"execution_incident_dispositions": 0},
            [("expired_attempts", 0, 0)],
        )
        with self.assertRaisesRegex(
            carry.CarryForwardError, "candidate_schema_inventory_changed"
        ):
            carry.validate_schema_inventory(
                "0038_issue138_languages",
                "0040_issue152_dispositions",
                baseline,
                expected | {"unapproved_table"},
                {"execution_incident_dispositions": 0},
                [("expired_attempts", 0, 0)],
            )

        revision = "0040_issue152_dispositions"
        same_schema = baseline | {
            "runtime_reconciliation_cursors",
            "execution_incident_dispositions",
        }
        carry.validate_schema_inventory(
            revision,
            revision,
            same_schema,
            same_schema,
            {"execution_incident_dispositions": 0},
            [],
        )
        for changed in (
            same_schema | {"unapproved_table"},
            same_schema - {"runtime_reconciliation_cursors"},
        ):
            with self.assertRaisesRegex(
                carry.CarryForwardError, "candidate_schema_inventory_changed"
            ):
                carry.validate_schema_inventory(
                    revision,
                    revision,
                    same_schema,
                    changed,
                    {"execution_incident_dispositions": 0},
                    [],
                )
        with self.assertRaisesRegex(carry.CarryForwardError, "candidate_seed_invalid"):
            carry.validate_schema_inventory(
                "0038_issue138_languages",
                "0040_issue152_dispositions",
                baseline,
                expected,
                {"execution_incident_dispositions": 0},
                [],
            )

    def test_transition_allowlist_keeps_all_forward_paths_and_rejects_unknowns(self):
        for transition in (
            ("0038_issue138_languages", "0039_issue134_reconcile"),
            ("0039_issue134_reconcile", "0040_issue152_dispositions"),
            ("0038_issue138_languages", "0040_issue152_dispositions"),
        ):
            self.assertTrue(carry.validate_schema_transition(*transition))
        self.assertEqual(
            carry.validate_schema_transition(
                "0040_issue152_dispositions", "0040_issue152_dispositions"
            ),
            set(),
        )
        for transition in (
            ("0041_unknown", "0041_unknown"),
            ("0040_issue152_dispositions", "0041_unknown"),
        ):
            with self.assertRaisesRegex(
                carry.CarryForwardError, "candidate_schema_path_unknown"
            ):
                carry.validate_schema_transition(*transition)

    def test_plan_capture_rejects_nonempty_audit_before_writing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            ids = root / "ids.json"
            carry.write_private(
                ids,
                {
                    "queued": [{"execution_id": 7, "incident_ids": [11]}],
                    "cleanup_execution_ids": [],
                },
            )
            args = SimpleNamespace(
                baseline=None,
                ids=ids,
                schema_phase=None,
                runtime_root=root / "runtime",
                journal_root=root / "journal",
                material_root=[],
                expected_uid=None,
                db_output=root / "db.json",
                files_output=root / "files.json",
            )
            with (
                mock.patch.object(
                    carry,
                    "inspect_database",
                    side_effect=carry.CarryForwardError("candidate_table_not_empty"),
                ),
                mock.patch.object(carry, "capture_files") as capture_files,
            ):
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "candidate_table_not_empty"
                ):
                    carry._command_capture_state(args)
            capture_files.assert_not_called()
            self.assertFalse(args.db_output.exists())
            self.assertFalse(args.files_output.exists())

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
            "format_version": carry.FORMAT_VERSION,
            "manifest_id": "1" * 32,
            "created_at": "2026-09-17T00:00:00+00:00",
            "repo": "owner/repo",
            "pr": 2,
            "from_sha": SHA_A,
            "to_sha": SHA_B,
            "from_schema": "0038_issue138_languages",
            "to_schema": "0040_issue152_dispositions",
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
            "schema_inventory": {"tables": sorted(carry.RESPONSIBILITY_TABLES)},
            "storage_identity": [],
            "old_containers": [],
            "file_evidence": {},
            "kernel_evidence": {},
        }
        manifest = carry.seal_manifest(payload)
        carry.validate_manifest(manifest)
        manifest["to_sha"] = SHA_A
        with self.assertRaisesRegex(
            carry.CarryForwardError, "manifest_digest_mismatch"
        ):
            carry.validate_manifest(manifest)

        for from_schema, to_schema in (
            ("0041_unknown", "0041_unknown"),
            ("0040_issue152_dispositions", "0041_unknown"),
        ):
            unknown = dict(payload, from_schema=from_schema, to_schema=to_schema)
            with self.assertRaisesRegex(
                carry.CarryForwardError, "candidate_schema_path_unknown"
            ):
                carry.seal_manifest(unknown)

    def test_private_file_requires_restricted_parent_and_regular_single_link(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o700)
            target = parent / "manifest.json"
            carry.write_private(target, {"safe": True})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(carry.read_private(target), {"safe": True})
            parent.chmod(0o755)
            with self.assertRaisesRegex(
                carry.CarryForwardError, "private_parent_invalid"
            ):
                carry.read_private(target)

    def test_kernel_compare_rejects_unknown_or_repopulated_children(self):
        baseline = {
            "boot_id": "boot",
            "unit": "dlr-test.service",
            "control_group": "/system.slice/dlr-test.service",
            "keeper_pid": 7,
            "keeper_starttime": "10",
            "children": {
                "agent": {
                    "populated": 1,
                    "process_count": 1,
                    "process_digest": carry.digest([7]),
                },
                "old-worker": {
                    "populated": 1,
                    "process_count": 1,
                    "process_digest": carry.digest([8]),
                },
            },
        }
        after = dict(
            baseline,
            children={"agent": baseline["children"]["agent"]},
            namespace_evidence={
                "related_count": 0,
                "pin_count": 0,
                "target_digest": "f" * 64,
            },
        )
        carry.compare_kernel(baseline, after)
        for name in ("unknown", "agent/hidden", "attempt-7-9"):
            children = dict(after["children"])
            children[name] = {
                "populated": 0,
                "process_count": 0,
                "process_digest": carry.digest([]),
            }
            with self.assertRaisesRegex(carry.CarryForwardError, "kernel_not_idle"):
                carry.compare_kernel(baseline, dict(after, children=children))


if __name__ == "__main__":
    unittest.main()
