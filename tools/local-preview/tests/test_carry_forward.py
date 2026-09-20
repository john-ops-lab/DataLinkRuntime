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


def precise_log_clock(timestamp):
    return {
        "kind": "precise-realtime",
        "lower_bound_ns": timestamp,
        "resolution_ns": 1,
    }


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


def group2_selection():
    queued = [
        {"execution_id": index, "incident_ids": [100 + index]} for index in range(1, 10)
    ]
    terminals = [
        {
            "execution_id": 20 + index,
            "incident_id": 220 + index,
            "disposition_id": str(uuid.UUID(int=500 + index)),
            "expected_status": "cancelled",
            "expected_generation": 1,
            "expected_output_digest": carry.digest(None),
            "expected_error_code": "execution_cancelled",
            "expected_last_error_code": "execution_cancelled",
            "expected_attempt_count": 0,
        }
        for index in range(5)
    ]
    return {
        "queued": queued,
        "cleanup_execution_ids": [20, 21, 22],
        "terminal_executions": terminals,
    }


def group2_scope():
    rules = carry._group2_expected_rules()
    entries = []
    for index, (path, rule) in enumerate(sorted(rules.items()), 1):
        status, old_mode, new_mode, old_oid, new_oid = rule
        entries.append(
            {
                "status": status,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid or ("0" * 40 if status == "A" else f"{index:040x}"),
                "new_oid": new_oid or f"{index + 1000:040x}",
                "path": path,
            }
        )
    source = {
        "from_sha": carry.GROUP2_FROM_SHA,
        "to_sha": SHA_B,
        "from_tree": carry.GROUP2_FROM_TREE,
        "to_tree": "c" * 40,
        "raw_diff_sha256": "d" * 64,
        "entries": entries,
    }
    source["tree_digest"] = carry.digest(
        {
            "from_tree": source["from_tree"],
            "to_tree": source["to_tree"],
            "entries": entries,
        }
    )
    anchor_entries = []
    for index, (path, rule) in enumerate(sorted(carry.GROUP2_PRODUCT_RULES.items()), 1):
        status, old_mode, new_mode, old_oid, new_oid = rule
        anchor_entries.append(
            {
                "status": status,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid
                or ("0" * 40 if status == "A" else f"{index + 2000:040x}"),
                "new_oid": new_oid or f"{index + 3000:040x}",
                "path": path,
            }
        )
    coverage = [{"path": item["path"], "blob_oid": item["new_oid"]} for item in entries]
    snapshot = {
        "selection": group2_selection(),
        "db": {},
        "files": {},
        "lineage": [{"name": "group1-final", "sha256": "e" * 64}],
    }
    files = {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in carry.GROUP2_CONTROLLER_FILES
    }
    scope = {
        "schema": carry.GROUP2_REVIEW_SCHEMA,
        "mode": carry.GROUP2_MODE,
        "repo": "owner/repo",
        "pr": 161,
        "approval": {
            "request_sha256": carry.GROUP2_REQUEST_DIGEST,
            "user_approval_sha256": "1" * 64,
            "product_scope_sha256": "2" * 64,
            "review_bindings_sha256": "3" * 64,
        },
        "product_anchor": {
            "base_sha": carry.GROUP2_FROM_SHA,
            "head_sha": carry.GROUP2_PRODUCT_SHA,
            "base_tree": carry.GROUP2_FROM_TREE,
            "head_tree": carry.GROUP2_PRODUCT_TREE,
            "raw_diff_sha256": carry.GROUP2_PRODUCT_RAW_DIGEST,
            "files": anchor_entries,
        },
        "final_source": source,
        "reviews": [
            {
                "name": f"review-{index}",
                "report_sha256": f"{index + 4:064x}",
                "reviewed_commit": SHA_B,
                "coverage": coverage if index == 3 else [],
                "status": "APPROVED",
            }
            for index in range(4)
        ],
        "controller_files": {"files": files, "digest": carry.digest(files)},
        "image_binding": {
            "old_image_ids": {"project-web:old": "sha256:" + "a" * 64},
            "candidate_image_ids": {"project-web:" + SHA_B: "sha256:" + "b" * 64},
        },
        "migration_graph": {
            "from_files": [{"path": "0040.py", "mode": "100644", "oid": "4" * 40}],
            "to_files": [{"path": "0040.py", "mode": "100644", "oid": "4" * 40}],
            "graph_digest": "5" * 64,
            "head": "0040_issue152_dispositions",
        },
        "ci": {
            "head_sha": SHA_B,
            "run_id": 9,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "jobs": [
                {"id": index + 1, "name": name, "conclusion": "success"}
                for index, name in enumerate(
                    ("backend", "compose-smoke", "local-preview", "web")
                )
            ],
            "evidence_sha256": "6" * 64,
        },
        "preservation_reference": {
            "snapshot": snapshot,
            "snapshot_digest": carry.digest(snapshot),
            "review_report_sha256": "7" * 64,
        },
    }
    scope["scope_digest"] = carry.digest(scope)
    return scope


def group2_manifest():
    scope = group2_scope()
    data, _ = audited_case()
    runtime_projection = carry.project_rows(data, required=carry.AUDITED_TABLES)
    asset_projection = {
        name: {"columns": ["id"], "primary_key": ["id"], "rows": [], "count": 0}
        for name in carry.ASSET_TABLES
    }
    protected = {
        "tables": {
            name: {
                "columns": runtime_projection[name]["columns"],
                "primary_key": runtime_projection[name]["primary_key"],
                "rows": [],
            }
            for name in carry.AUDITED_TABLES
        },
        "adapter_ids": [],
        "version_ids": [],
        "execution_ids": [],
        "attempt_ids": [],
    }
    inventory = sorted(set(carry.AUDITED_TABLES) | set(carry.ASSET_TABLES))
    shape = {
        name: {
            "columns": [
                {"name": "id", "type": "BIGINT", "nullable": False, "default": None}
            ],
            "primary_key": ["id"],
        }
        for name in inventory
    }
    candidate_profiles = {}
    log_files = []
    log_roots = []
    for service in ("control", "worker", "web", "account-web"):
        root = f"/private/logs/{service}"
        names = (
            ["access.log", "error.log"]
            if service in {"web", "account-web"}
            else [f"{service}.log"]
        )
        candidate_profiles[service] = {
            "image": "image",
            "command": [service],
            "effective_command": [None, [service]],
            "networks": ["dlr_default"],
            "mounts": [
                {
                    "type": "bind",
                    "source": root,
                    "destination": f"/var/lib/dlr/platform-logs/{service}",
                    "rw": True,
                }
            ],
            "ports": [],
        }
        log_roots.append({"path": root, "allowed_new_files": names})
        log_files.extend(f"{root}/{name}" for name in names)
    candidate_profiles["web"]["ports"] = [
        {
            "host_ip": "127.0.0.1",
            "published": "8080",
            "target": 80,
            "protocol": "tcp",
        }
    ]
    candidate_profiles["account-web"]["ports"] = [
        {
            "host_ip": "127.0.0.1",
            "published": "8081",
            "target": 80,
            "protocol": "tcp",
        }
    ]
    profile = {
        "project": "dlr",
        "from_sha": carry.GROUP2_FROM_SHA,
        "to_sha": SHA_B,
        "old_containers": {
            service: {
                "container_id": "old-" + service,
                "image_id": "old-image",
                "status": "exited",
                "health": "healthy",
                "started_at": "2026-09-19T00:00:00Z",
                "restart_count": 0,
                "command": [None, [service]],
                "labels": {
                    "com.docker.compose.project": "dlr",
                    "com.docker.compose.service": service,
                },
                "port_bindings": {},
                "mounts": candidate_profiles[service]["mounts"],
                "networks": candidate_profiles[service]["networks"],
            }
            for service in candidate_profiles
        },
        "old_profiles": copy.deepcopy(candidate_profiles),
        "candidate_profiles": candidate_profiles,
        "candidate_web_image_id": "sha256:" + "b" * 64,
        "candidate_image_ids_by_service": {
            service: "sha256:" + "b" * 64
            for service in ("control", "worker", "web", "account-web")
        },
        "ports": {
            "account": candidate_profiles["account-web"]["ports"][0],
            "token": candidate_profiles["web"]["ports"],
        },
        "log_files": sorted(log_files),
        "log_roots": sorted(log_roots, key=lambda item: item["path"]),
    }
    profile["profile_digest"] = carry.digest(profile)
    logs = {
        "profile_digest": profile["profile_digest"],
        "files": [],
        "roots": [],
        "clock": precise_log_clock(1),
        "observed_at_ns": 1,
    }
    logs["evidence_digest"] = carry.digest(logs)
    payload = {
        "format_version": carry.GROUP2_FORMAT_VERSION,
        "mode": carry.GROUP2_MODE,
        "source_diff": scope["final_source"],
        "review_scope": scope,
        "review_scope_digest": scope["scope_digest"],
        "ci_binding": scope["ci"],
        "preservation_reference_digest": scope["preservation_reference"][
            "snapshot_digest"
        ],
        "protected_rows": protected,
        "asset_projection": asset_projection,
        "schema_shape": shape,
        "account_entry": profile,
        "log_evidence": logs,
        "manifest_id": "1" * 32,
        "created_at": "2026-09-20T00:00:00+00:00",
        "repo": scope["repo"],
        "pr": scope["pr"],
        "from_sha": carry.GROUP2_FROM_SHA,
        "to_sha": SHA_B,
        "from_schema": "0040_issue152_dispositions",
        "to_schema": "0040_issue152_dispositions",
        "controller_files_digest": scope["controller_files"]["digest"],
        "migration_graph_digest": scope["migration_graph"]["graph_digest"],
        "old_image_ids": scope["image_binding"]["old_image_ids"],
        "candidate_image_ids": scope["image_binding"]["candidate_image_ids"],
        "selection": group2_selection(),
        "responsibilities": {
            "executions": [],
            "terminal_executions": [{} for _ in range(5)],
        },
        "old_runtime_projection": runtime_projection,
        "schema_inventory": {"tables": inventory},
        "storage_identity": [],
        "old_containers": [],
        "file_evidence": {},
        "kernel_evidence": {},
    }
    return carry.seal_manifest(payload)


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

    def test_group2_selection_is_exact_q9_c3_t5_and_allows_terminal_cleanup_overlap(
        self,
    ):
        value = group2_selection()
        self.assertEqual(
            carry.normalize_selection(value, mode=carry.GROUP2_MODE), value
        )
        for key in ("queued", "cleanup_execution_ids", "terminal_executions"):
            changed = copy.deepcopy(value)
            changed[key].pop()
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "group2_selection_count_invalid"
                ),
            ):
                carry.normalize_selection(changed, mode=carry.GROUP2_MODE)


class Group2ScopeTests(unittest.TestCase):
    def test_scope_and_exact_64_path_source_are_closed(self):
        scope = group2_scope()
        self.assertIs(carry.validate_group2_review_scope(scope), scope)
        self.assertEqual(len(scope["final_source"]["entries"]), 64)
        self.assertIs(
            carry.validate_group2_source_diff(scope["final_source"], scope),
            scope["final_source"],
        )
        self.assertEqual(len(carry.GROUP2_PRODUCT_RULES), 57)
        self.assertEqual(
            sum(
                rule[3] is not None and rule[4] is not None
                for rule in carry.GROUP2_PRODUCT_RULES.values()
            ),
            54,
        )
        self.assertEqual(len(carry._group2_expected_rules()), 64)

    def test_scope_rejects_missing_duplicate_and_unreviewed_product_blob(self):
        for mutate, code in (
            (
                lambda scope: scope["product_anchor"]["files"].__setitem__(
                    1, copy.deepcopy(scope["product_anchor"]["files"][0])
                ),
                "group2_product_anchor_invalid",
            ),
            (
                lambda scope: scope["final_source"]["entries"][0].__setitem__(
                    "new_oid", "f" * 40
                ),
                "group2_source_diff_invalid",
            ),
        ):
            scope = group2_scope()
            mutate(scope)
            scope["scope_digest"] = carry.digest(
                {key: item for key, item in scope.items() if key != "scope_digest"}
            )
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(carry.CarryForwardError, code),
            ):
                carry.validate_group2_review_scope(scope)

    def test_assets_table_contract_includes_all_assets_and_sessions_without_import(
        self,
    ):
        import ast

        source = Path(carry.__file__).with_name("assets.py").read_text()
        module = ast.parse(source)
        tables_value = next(
            ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "TABLES"
                for target in node.targets
            )
        )
        self.assertEqual(carry.ASSET_TABLES, (*tables_value, "user_sessions"))

    def test_v4_manifest_is_closed_and_v3_cannot_receive_group2_fields(self):
        manifest = group2_manifest()
        self.assertIs(carry.validate_manifest(manifest), manifest)
        for key in ("schema_shape", "review_scope", "account_entry"):
            changed = copy.deepcopy(manifest)
            changed.pop(key)
            changed["manifest_digest"] = carry.digest(carry.manifest_payload(changed))
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "manifest_shape_invalid"
                ),
            ):
                carry.validate_manifest(changed)
        changed = copy.deepcopy(manifest)
        changed["format_version"] = carry.AUDITED_FORMAT_VERSION
        changed["mode"] = carry.AUDITED_MODE
        changed["manifest_digest"] = carry.digest(carry.manifest_payload(changed))
        with self.assertRaisesRegex(carry.CarryForwardError, "manifest_shape_invalid"):
            carry.validate_manifest(changed)

    def test_protected_rows_reject_duplicate_or_unsorted_primary_keys(self):
        protected = group2_manifest()["protected_rows"]
        row = {"pk": {"id": 7}, "hash": "a" * 64}
        for rows in ([row, copy.deepcopy(row)], [{**row, "pk": {"id": 8}}, row]):
            changed = copy.deepcopy(protected)
            changed["tables"]["executions"]["rows"] = rows
            with self.assertRaisesRegex(
                carry.CarryForwardError, "protected_rows_invalid"
            ):
                carry.validate_group2_protected_rows(changed)


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
                data["executions"]["rows"][0]["workspace_cleanup_status"] = (
                    execution_cleanup
                )
                data["execution_attempts"]["rows"][0]["cleanup_summary"] = (
                    attempt_cleanup
                )
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
            with (
                self.subTest(columns=changed),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "projection_columns_changed"
                ),
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

    def _startup_proof(self, start, end):
        nonce = "a" * 32
        capabilities = {
            name: True
            for name in (
                "adapter_control_plane_hidden",
                "adapter_mount_blocked",
                "bounded_output",
                "cgroup_kill",
                "cgroup_namespace_private",
                "cgroup_v2",
                "cpu_hard_limit",
                "memory_hard_limit",
                "mount_namespace",
                "no_new_privileges",
                "nofile_hard_limit",
                "pid_namespace",
                "pids_hard_limit",
                "preflight_passed",
                "sandbox_cleanup",
                "swap_hard_limit",
                "tmpfs_hard_limit",
            )
        }
        return {
            "container_id": "container-new",
            "image_id": "sha256:" + "b" * 64,
            "started_at": "2026-09-20T00:00:00Z",
            "restart_count": 0,
            "nonce": nonce,
            "window_start_ns": start,
            "window_end_ns": end,
            "preflight_receipt": {
                "cgroup_name": f"dlr-preflight-{nonce}",
                "status": "passed",
                "workspace_residue": False,
                "capabilities": capabilities,
                "adapter_control_pipe_fds": [],
                "adapter_hidden_cgroup_paths": {
                    "/run/dlr-cgroup": {
                        "read_blocked": True,
                        "write_blocked": True,
                    },
                    "/sys/fs/cgroup": {
                        "read_blocked": True,
                        "write_blocked": True,
                    },
                },
                "agent_outside_attempt": True,
                "helper_outside_attempt": True,
                "probe_in_attempt": True,
                "child_empty_after_kill": True,
                "process_exited_after_kill": True,
                "worker_cgroup_management": {
                    "child_limit_write_read": True,
                    "parent_controllers_read": True,
                },
                "namespace_identity": {
                    "boot_id": "boot",
                    "parent_device": 1,
                    "parent_inode": 2,
                    "root_device": 1,
                    "root_inode": 3,
                },
                "cleanup": {
                    "cgroup_name": f"dlr-preflight-{nonce}",
                    "status": "completed",
                    "residue": False,
                    "error_code": None,
                },
            },
            "log_evidence_digest": "f" * 64,
        }

    def test_group2_startup_allows_only_two_window_bound_mtimes(self):
        before = carry.capture_files(self.runtime, self.journal)
        start = (
            max(
                before["runtime"]["root"]["mtime_ns"],
                next(
                    item
                    for item in before["journal"]["entries"]
                    if item["path"] == "sandbox-recovery"
                )["mtime_ns"],
            )
            + 1_000_000
        )
        os.utime(self.runtime, ns=(start, start))
        recovery = self.journal / "sandbox-recovery"
        os.utime(recovery, ns=(start + 1, start + 1))
        after = carry.capture_files(self.runtime, self.journal)
        original = copy.deepcopy(before), copy.deepcopy(after)
        result = carry.compare_group2_startup_files(
            before, after, self._startup_proof(start, start + 10)
        )
        self.assertEqual(result["code"], "group2_startup_files_ok")
        self.assertEqual((before, after), original)
        changed = copy.deepcopy(after)
        changed["runtime"]["entries"][0]["mtime_ns"] += 1
        changed["runtime"]["digest"] = carry.digest(changed["runtime"]["entries"])
        with self.assertRaisesRegex(
            carry.CarryForwardError, "group2_startup_files_changed"
        ):
            carry.compare_group2_startup_files(
                before, changed, self._startup_proof(start, start + 10)
            )

    def test_group2_probe_requires_owned_empty_shell_identity(self):
        before = carry.capture_files(self.runtime, self.journal)
        after = copy.deepcopy(before)
        workspaces = next(
            item for item in after["runtime"]["entries"] if item["path"] == "workspaces"
        )
        created = dict(
            workspaces,
            path="workspaces/attempt-99",
            mtime_ns=workspaces["mtime_ns"] + 1,
        )
        after["runtime"]["entries"].append(created)
        after["runtime"]["entries"].sort(key=lambda item: item["path"])
        after["runtime"]["digest"] = carry.digest(after["runtime"]["entries"])
        after["empty_attempt_shells"] = [
            {"attempt_id": 99, "classification": "owned_empty_shell_without_db_row"}
        ]
        cleanup_row = {
            "id": 1,
            "adapter_id": 77,
            "worker_id": 88,
            "status": "completed",
            "attempts": 1,
            "error_code": None,
            "created_at": {"$datetime": "2026-09-20T00:00:00+00:00"},
            "updated_at": {"$datetime": "2026-09-20T00:00:01+00:00"},
            "completed_at": {"$datetime": "2026-09-20T00:00:01+00:00"},
        }
        proof = {
            "adapter_id": 77,
            "execution_id": 78,
            "worker_id": 88,
            "attempt_id": 99,
            "window_start_ns": workspaces["mtime_ns"],
            "window_end_ns": workspaces["mtime_ns"] + 10,
            "probe_result": {
                "execution_id": 78,
                "status": "succeeded",
                "workspace_cleanup_status": "completed",
            },
            "cleanup": {"row": cleanup_row, "row_sha256": carry.digest(cleanup_row)},
            "log_evidence_digest": "e" * 64,
            "event_refs": [{"offset": 0, "line_sha256": "d" * 64}],
        }
        self.assertEqual(
            carry.compare_group2_probe_files(before, after, proof)["code"],
            "group2_probe_files_ok",
        )
        after["runtime"]["entries"][-1]["uid"] += 1
        after["runtime"]["digest"] = carry.digest(after["runtime"]["entries"])
        with self.assertRaisesRegex(
            carry.CarryForwardError, "group2_probe_files_changed"
        ):
            carry.compare_group2_probe_files(before, after, proof)

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
                self.assertRaisesRegex(
                    carry.CarryForwardError, "candidate_table_not_empty"
                ),
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


class Group2RuntimeTests(unittest.TestCase):
    def _reseal_log(self, evidence):
        evidence["evidence_digest"] = carry.digest(
            {key: item for key, item in evidence.items() if key != "evidence_digest"}
        )

    def _replace_log_text(self, evidence, text):
        item = evidence["files"][0]
        item["appended_text"] = text
        item["end_size"] = item["size"] + len(text.encode())
        if item["size"] == 0:
            item["end_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        self._reseal_log(evidence)

    def _log_window(self, text, size=0):
        empty_digest = hashlib.sha256(b"").hexdigest()
        prefix_digest = empty_digest if size == 0 else "a" * 64
        common = {
            "path": "/logs/control/control.log",
            "exists": True,
            "device": 1,
            "inode": 2,
            "mode": 0o640,
            "uid": 3,
            "gid": 4,
            "size": size,
            "prefix_sha256": prefix_digest,
        }
        before = {
            "profile_digest": "b" * 64,
            "files": [common],
            "roots": [],
            "clock": precise_log_clock(10),
            "observed_at_ns": 10,
        }
        before["evidence_digest"] = carry.digest(before)
        after = {
            "profile_digest": before["profile_digest"],
            "files": [
                {
                    **common,
                    "appended_text": text,
                    "end_size": size + len(text.encode()),
                    "end_sha256": (
                        hashlib.sha256(text.encode()).hexdigest()
                        if size == 0
                        else "c" * 64
                    ),
                }
            ],
            "roots": [],
            "clock": precise_log_clock(20),
            "baseline_evidence_digest": before["evidence_digest"],
            "observed_after_ns": 20,
        }
        after["evidence_digest"] = carry.digest(after)
        return before, after

    def _profile(self, path):
        base = path.parent.parent if path.parent.name == "worker" else path.parent
        candidate_profiles = {}
        log_files = []
        log_roots = []
        for service in ("control", "worker", "web", "account-web"):
            root = base / service
            root.mkdir(parents=True, exist_ok=True)
            names = (
                ["access.log", "error.log"]
                if service in {"web", "account-web"}
                else [f"{service}.log"]
            )
            candidate_profiles[service] = {
                "image": f"dlr-{service}",
                "command": [service],
                "effective_command": [None, [service]],
                "networks": ["dlr_default"],
                "mounts": [
                    {
                        "type": "bind",
                        "source": str(root),
                        "destination": f"/var/lib/dlr/platform-logs/{service}",
                        "rw": True,
                    }
                ],
                "ports": [],
            }
            log_roots.append({"path": str(root), "allowed_new_files": sorted(names)})
            log_files.extend(str(root / name) for name in names)
        candidate_profiles["web"]["ports"] = [
            {
                "host_ip": "127.0.0.1",
                "published": "8080",
                "target": 80,
                "protocol": "tcp",
            }
        ]
        candidate_profiles["account-web"]["ports"] = [
            {
                "host_ip": "127.0.0.1",
                "published": "8081",
                "target": 80,
                "protocol": "tcp",
            }
        ]
        value = {
            "project": "dlr",
            "from_sha": SHA_A,
            "to_sha": SHA_B,
            "old_containers": {
                service: {
                    "container_id": "old-" + service,
                    "image_id": "old-image",
                    "status": "exited",
                    "health": "healthy",
                    "started_at": "2026-09-19T00:00:00Z",
                    "restart_count": 0,
                    "command": [None, [service]],
                    "labels": {
                        "com.docker.compose.project": "dlr",
                        "com.docker.compose.service": service,
                    },
                    "port_bindings": {},
                    "mounts": candidate_profiles[service]["mounts"],
                    "networks": candidate_profiles[service]["networks"],
                }
                for service in candidate_profiles
            },
            "old_profiles": copy.deepcopy(candidate_profiles),
            "candidate_profiles": candidate_profiles,
            "candidate_web_image_id": "sha256:" + "b" * 64,
            "candidate_image_ids_by_service": {
                service: "sha256:" + "b" * 64
                for service in ("control", "worker", "web", "account-web")
            },
            "ports": {
                "account": candidate_profiles["account-web"]["ports"][0],
                "token": candidate_profiles["web"]["ports"],
            },
            "log_files": sorted(log_files),
            "log_roots": sorted(log_roots, key=lambda item: item["path"]),
        }
        value["profile_digest"] = carry.digest(value)
        return value

    def test_log_prefix_append_preserves_original_bytes_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker" / "worker.log"
            path.parent.mkdir()
            path.write_text("before\n")
            profile = self._profile(path)
            baseline = carry.capture_log_prefix(profile)
            with path.open("a") as stream:
                stream.write("after\n")
            evidence = carry.read_log_append(baseline)
            worker = next(
                item for item in evidence["files"] if item["path"] == str(path)
            )
            self.assertEqual(worker["appended_text"], "after\n")
            self.assertEqual(
                worker["end_sha256"],
                hashlib.sha256(b"before\nafter\n").hexdigest(),
            )
            self.assertEqual(
                evidence["baseline_evidence_digest"], baseline["evidence_digest"]
            )
            with path.open("a") as stream:
                stream.write("later\n")
            chained = carry.read_log_append(evidence)
            chained_worker = next(
                item for item in chained["files"] if item["path"] == str(path)
            )
            self.assertEqual(chained_worker["appended_text"], "later\n")
            self.assertEqual(
                chained["baseline_evidence_digest"], evidence["evidence_digest"]
            )
            carry._validate_log_link(evidence, chained, profile["profile_digest"])
            for mutate in (
                lambda value: next(
                    item for item in value["files"] if item["path"] == str(path)
                ).__setitem__("prefix_sha256", "f" * 64),
                lambda value: next(
                    item for item in value["files"] if item["path"] == str(path)
                ).__setitem__("inode", 999999999),
                lambda value: value["roots"][0].__setitem__(
                    "mtime_ns", value["roots"][0]["mtime_ns"] + 1
                ),
            ):
                changed = copy.deepcopy(chained)
                mutate(changed)
                changed["evidence_digest"] = carry.digest(
                    {
                        key: item
                        for key, item in changed.items()
                        if key != "evidence_digest"
                    }
                )
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "log_evidence_link_invalid"
                ):
                    carry._validate_log_link(
                        evidence, changed, profile["profile_digest"]
                    )
            complete = carry.combine_group2_log_window(baseline, evidence, chained)
            complete_worker = next(
                item for item in complete["files"] if item["path"] == str(path)
            )
            self.assertEqual(complete_worker["appended_text"], "after\nlater\n")
            self.assertEqual(
                complete["baseline_evidence_digest"], baseline["evidence_digest"]
            )
            carry._validate_log_link(baseline, complete, profile["profile_digest"])
            invalid = copy.deepcopy(chained)
            invalid_worker = next(
                item for item in invalid["files"] if item["path"] == str(path)
            )
            invalid_worker["end_size"] += 1
            invalid["evidence_digest"] = carry.digest(
                {key: item for key, item in invalid.items() if key != "evidence_digest"}
            )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_invalid"
            ):
                carry.combine_group2_log_window(baseline, evidence, invalid)
            path.write_text("replaced\n")
            with self.assertRaisesRegex(carry.CarryForwardError, "log_prefix_changed"):
                carry.read_log_append(baseline)

    def test_log_roots_reject_unknown_paths_symlinks_and_directory_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker" / "worker.log"
            path.parent.mkdir()
            path.write_text("before\n")
            profile = self._profile(path)
            baseline = carry.capture_log_prefix(profile)
            (path.parent / "unknown.log").write_text("unexpected\n")
            with self.assertRaisesRegex(carry.CarryForwardError, "log_unknown_path"):
                carry.read_log_append(baseline)
            (path.parent / "unknown.log").unlink()
            path.parent.chmod(0o777)
            with self.assertRaisesRegex(carry.CarryForwardError, "log_root_changed"):
                carry.read_log_append(baseline)
            path.parent.chmod(0o755)
            missing = Path(directory) / "account-web" / "access.log"
            missing.symlink_to(path)
            with self.assertRaisesRegex(carry.CarryForwardError, "log_file_invalid"):
                carry.read_log_append(baseline)

    def test_log_directory_mtime_requires_a_new_approved_direct_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "worker" / "worker.log"
            worker.parent.mkdir()
            worker.write_text("before\n")
            archive = worker.parent / "archive"
            archive.mkdir()
            profile = self._profile(worker)

            baseline = carry.capture_log_prefix(profile)
            worker_root = next(
                item for item in baseline["roots"] if item["path"] == str(worker.parent)
            )
            self.assertIn("mtime_ns", worker_root)
            self.assertIn(
                "mtime_ns",
                next(
                    item for item in worker_root["entries"] if item["path"] == "archive"
                ),
            )

            old_root_mtime = worker.parent.stat().st_mtime_ns
            os.utime(
                worker.parent,
                ns=(worker.parent.stat().st_atime_ns, old_root_mtime + 1),
            )
            with self.assertRaisesRegex(carry.CarryForwardError, "log_root_changed"):
                carry.read_log_append(baseline)
            os.utime(
                worker.parent, ns=(worker.parent.stat().st_atime_ns, old_root_mtime)
            )

            old_nested_mtime = archive.stat().st_mtime_ns
            os.utime(
                archive,
                ns=(archive.stat().st_atime_ns, old_nested_mtime + 1),
            )
            with self.assertRaisesRegex(carry.CarryForwardError, "log_root_changed"):
                carry.read_log_append(baseline)
            os.utime(archive, ns=(archive.stat().st_atime_ns, old_nested_mtime))

            account_access = root / "account-web" / "access.log"
            account_access.write_text("created\n")
            account_root = account_access.parent
            new_root_mtime = account_root.stat().st_mtime_ns
            account_root_before = next(
                item
                for item in baseline["roots"]
                if item["path"] == str(account_root)
            )
            self.assertGreaterEqual(new_root_mtime, account_root_before["mtime_ns"])
            baseline["clock"] = {
                "kind": "linux-realtime-coarse",
                "lower_bound_ns": account_root_before["mtime_ns"],
                "resolution_ns": 1_000_000,
            }
            baseline["observed_at_ns"] = new_root_mtime + 500_000
            baseline["evidence_digest"] = carry.digest(
                {
                    key: item
                    for key, item in baseline.items()
                    if key != "evidence_digest"
                }
            )
            after_clock = {
                "kind": "linux-realtime-coarse",
                "lower_bound_ns": new_root_mtime,
                "resolution_ns": 1_000_000,
            }
            with (
                mock.patch.object(
                    carry,
                    "_log_observation",
                    return_value=(
                        precise_log_clock(new_root_mtime + 1_000_000),
                        new_root_mtime + 1_000_000,
                    ),
                ),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "log_evidence_link_invalid"
                ),
            ):
                carry.read_log_append(baseline)
            with mock.patch.object(
                carry,
                "_log_observation",
                return_value=(after_clock, new_root_mtime + 1_000_000),
            ):
                first = carry.read_log_append(baseline)
            carry._validate_log_link(baseline, first, profile["profile_digest"])
            self.assertEqual(
                next(
                    item
                    for item in first["files"]
                    if item["path"] == str(account_access)
                )["appended_text"],
                "created\n",
            )
            changed = copy.deepcopy(first)
            changed["clock"]["lower_bound_ns"] = (
                baseline["clock"]["lower_bound_ns"] - 1
            )
            changed["evidence_digest"] = carry.digest(
                {
                    key: item
                    for key, item in changed.items()
                    if key != "evidence_digest"
                }
            )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_link_invalid"
            ):
                carry._validate_log_link(baseline, changed, profile["profile_digest"])
            with self.assertRaisesRegex(carry.CarryForwardError, "log_clock_invalid"):
                carry._validate_log_clock(
                    {
                        "kind": "linux-realtime-coarse",
                        "lower_bound_ns": -1,
                        "resolution_ns": 1_000_000,
                    },
                    0,
                )
            os.utime(
                account_root,
                ns=(
                    account_root.stat().st_atime_ns,
                    account_root.stat().st_mtime_ns + 1,
                ),
            )
            with self.assertRaisesRegex(carry.CarryForwardError, "log_root_changed"):
                carry.read_log_append(first)

    def test_group2_runtime_rejects_unknown_operation_and_fields(self):
        with self.assertRaisesRegex(
            carry.CarryForwardError, "group2_runtime_operation_invalid"
        ):
            carry.group2_runtime(
                {"mode": carry.GROUP2_MODE, "operation": "write-ready"}
            )

    def test_receipt_and_recovery_validators_recompute_raw_evidence(self):
        manifest = group2_manifest()

        def append(previous, timestamp):
            value = {
                "profile_digest": previous["profile_digest"],
                "files": [],
                "roots": [],
                "clock": precise_log_clock(timestamp),
                "baseline_evidence_digest": previous["evidence_digest"],
                "observed_after_ns": timestamp,
            }
            value["evidence_digest"] = carry.digest(value)
            return value

        stage_inputs = {}
        startup_before = manifest["log_evidence"]
        for timestamp, name in enumerate(
            (
                "preflight",
                "control_stopped",
                "stopped",
                "after_backup",
                "after_migration",
            ),
            2,
        ):
            startup_before = append(startup_before, timestamp)
            stage_inputs[name] = {
                "db": {"stage": name},
                "files": {},
                "logs_after": startup_before,
            }
        with (
            mock.patch.object(carry, "_validate_group2_manifest_db"),
            mock.patch.object(carry, "_validate_capture_digests"),
        ):
            self.assertEqual(
                carry._validate_group2_stage_inputs(manifest, stage_inputs)[-1],
                startup_before,
            )
            broken_stages = copy.deepcopy(stage_inputs)
            broken = broken_stages["control_stopped"]["logs_after"]
            broken["baseline_evidence_digest"] = "f" * 64
            broken["evidence_digest"] = carry.digest(
                {key: item for key, item in broken.items() if key != "evidence_digest"}
            )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_link_invalid"
            ):
                carry._validate_group2_stage_inputs(manifest, broken_stages)
        startup_after = append(startup_before, 7)
        probe_before = append(startup_after, 8)
        probe_partial = append(probe_before, 9)
        probe_final = append(probe_partial, 10)
        probe_complete = carry.combine_group2_log_window(
            probe_before, probe_partial, probe_final
        )
        health_after = append(probe_final, 11)
        worker_lifetime = {
            "container_id": "worker-current",
            "image_id": "worker-image",
            "started_at": "2026-09-20T00:00:00Z",
            "restart_count": 0,
        }
        startup_request = {
            "mode": carry.GROUP2_MODE,
            "operation": "startup-proof",
            "profile": manifest["account_entry"],
            "logs_before": startup_before,
            "logs_after": startup_after,
            "container_before": manifest["account_entry"]["old_containers"]["worker"],
            "container_after": worker_lifetime,
            "window_start_ns": 1,
            "window_end_ns": 2,
        }
        startup_proof = {"proof": "startup"}
        startup_result = {"code": "startup-ok"}
        cleanup = {"row": {}, "row_sha256": "a" * 64}
        partial_proof = {
            "adapter_id": 1,
            "execution_id": 2,
            "worker_id": 3,
            "attempt_id": 4,
        }
        final_proof = {**partial_proof, "cleanup": cleanup}
        post_result = {"code": "post-ok"}
        account_check = {
            "containers": {"worker": copy.deepcopy(worker_lifetime)}
        }
        entry_probe = {"entry": "ok"}
        probe = {
            "proof": final_proof,
            "before_db": {"state": "started"},
            "after_db": {"state": "final"},
            "before_files": {"state": "started"},
            "after_files": {"state": "final"},
            "logs_before": probe_before,
            "logs_partial": probe_partial,
            "logs_final": probe_final,
            "logs_complete": probe_complete,
            "probe_result": {"status": "succeeded"},
            "cleanup": cleanup,
            "result": post_result,
        }
        startup = {
            "request": startup_request,
            "proof": startup_proof,
            "before_files": manifest["file_evidence"],
            "after_files": probe["before_files"],
            "after_db": probe["before_db"],
            "result": startup_result,
        }
        post_health = {
            "account_check": account_check,
            "entry_probe": entry_probe,
            "logs_before": probe_final,
            "logs_after": health_after,
        }
        stages = {
            "preflight": stage_inputs["preflight"]["db"],
            "control_stopped": stage_inputs["control_stopped"]["db"],
            "stopped": stage_inputs["stopped"]["db"],
            "backup": {
                "db": stage_inputs["after_backup"]["db"],
                "dump_sha256": "b" * 64,
                "list_sha256": "c" * 64,
            },
            "same_schema": stage_inputs["after_migration"]["db"],
            "started": startup_result,
            "probe": probe["probe_result"],
            "natural_cleanup": cleanup,
            "post_preservation": {
                "result": post_result,
                "db": probe["after_db"],
                "files": probe["after_files"],
            },
            "post_health": {
                "account_check": account_check,
                "entry_probe": entry_probe,
                "logs_after": health_after,
            },
        }
        evidence = {
            "stages": stages,
            "stage_inputs": stage_inputs,
            "startup": startup,
            "probe": probe,
            "post_health": post_health,
        }
        patches = (
            mock.patch.object(carry, "_validate_group2_manifest_db"),
            mock.patch.object(carry, "_validate_capture_digests"),
            mock.patch.object(carry, "_startup_proof", return_value=startup_proof),
            mock.patch.object(
                carry, "compare_group2_startup_files", return_value=startup_result
            ),
            mock.patch.object(
                carry,
                "derive_group2_probe_provenance",
                side_effect=[partial_proof, final_proof],
            ),
            mock.patch.object(
                carry, "compare_group2_post_probe", return_value=post_result
            ),
            mock.patch.object(
                carry, "validate_group2_account_check", return_value=account_check
            ),
            mock.patch.object(
                carry, "validate_group2_entry_probe", return_value=entry_probe
            ),
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            result = carry.validate_group2_receipt_evidence(manifest, evidence)
        self.assertTrue(result["cleanup_ready"])
        self.assertEqual(
            set(result),
            {
                "startup_result",
                "post_result",
                "account_ready",
                "entry_ready",
                "cleanup_ready",
                "log_chain_digest",
            },
        )

        changed = copy.deepcopy(evidence)
        changed["post_health"]["account_check"]["containers"]["worker"][
            "restart_count"
        ] = 1
        changed["stages"]["post_health"]["account_check"] = changed[
            "post_health"
        ]["account_check"]
        with (
            mock.patch.object(carry, "_validate_group2_manifest_db"),
            mock.patch.object(carry, "_validate_capture_digests"),
            mock.patch.object(carry, "_startup_proof", return_value=startup_proof),
            mock.patch.object(
                carry, "compare_group2_startup_files", return_value=startup_result
            ),
            mock.patch.object(
                carry,
                "derive_group2_probe_provenance",
                side_effect=[partial_proof, final_proof],
            ),
            mock.patch.object(
                carry, "compare_group2_post_probe", return_value=post_result
            ),
            mock.patch.object(
                carry,
                "validate_group2_account_check",
                return_value=changed["post_health"]["account_check"],
            ),
            mock.patch.object(
                carry, "validate_group2_entry_probe", return_value=entry_probe
            ),
            self.assertRaisesRegex(
                carry.CarryForwardError, "group2_worker_lifetime_changed"
            ),
        ):
            carry.validate_group2_receipt_evidence(manifest, changed)

        changed = copy.deepcopy(evidence)
        changed["probe"]["logs_before"]["baseline_evidence_digest"] = "f" * 64
        changed["probe"]["logs_before"]["evidence_digest"] = carry.digest(
            {
                key: item
                for key, item in changed["probe"]["logs_before"].items()
                if key != "evidence_digest"
            }
        )
        with (
            mock.patch.object(carry, "_validate_group2_manifest_db"),
            mock.patch.object(carry, "_validate_capture_digests"),
            mock.patch.object(carry, "_startup_proof", return_value=startup_proof),
            mock.patch.object(
                carry, "compare_group2_startup_files", return_value=startup_result
            ),
            self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_link_invalid"
            ),
        ):
            carry.validate_group2_receipt_evidence(manifest, changed)

        db = {
            key: value
            for key, value in {
                "projection": {},
                "responsibilities": {},
                "protected_rows": {},
                "asset_projection": {},
                "schema_shape": {},
                "schema_inventory": {},
            }.items()
        }
        recovery_anchor = health_after
        recovery_before = append(recovery_anchor, 12)
        recovery_after = append(recovery_before, 13)
        recovery_baseline = {
            "deployment": {
                "db": db,
                "files": {},
                "logs_after": recovery_anchor,
                "post_preservation": {
                    "code": "group2_post_probe_ok",
                    "cleanup_row_sha256": "d" * 64,
                    "post_db_digest": carry.digest(
                        {
                            "protected_rows": db["protected_rows"],
                            "asset_projection": db["asset_projection"],
                            "schema_shape": db["schema_shape"],
                        }
                    ),
                    "file_delta_digest": "e" * 64,
                },
            },
            "predecessor": {
                "kind": "deployment",
                "recovery_id": None,
                "evidence_digest": None,
                "db": copy.deepcopy(db),
                "files": {},
                "logs_after": recovery_anchor,
            },
            "fresh": {"db": copy.deepcopy(db), "files": {}},
        }
        before_worker = copy.deepcopy(
            manifest["account_entry"]["old_containers"]["worker"]
        )
        before_worker["image_id"] = manifest["account_entry"][
            "candidate_image_ids_by_service"
        ]["worker"]
        after_worker = copy.deepcopy(before_worker)
        after_worker.update(container_id="recovered-worker", status="running")
        recovery_request = {
            **startup_request,
            "logs_before": recovery_before,
            "logs_after": recovery_after,
            "container_before": before_worker,
            "container_after": after_worker,
        }
        recovery_account = {"containers": {"worker": after_worker}}
        recovery = {
            "request": recovery_request,
            "proof": startup_proof,
            "before_db": db,
            "after_db": copy.deepcopy(db),
            "before_files": {},
            "after_files": {"state": "recovered"},
            "account_check": recovery_account,
            "entry_probe": entry_probe,
            "logs_before": recovery_before,
            "logs_after": recovery_after,
            "preservation": startup_result,
        }
        with (
            mock.patch.object(carry, "_validate_capture_digests"),
            mock.patch.object(carry, "_startup_proof", return_value=startup_proof),
            mock.patch.object(
                carry, "compare_group2_startup_files", return_value=startup_result
            ),
            mock.patch.object(
                carry, "validate_group2_account_check", return_value=recovery_account
            ),
            mock.patch.object(
                carry, "validate_group2_entry_probe", return_value=entry_probe
            ),
        ):
            recovered = carry.validate_group2_recovery_evidence(
                manifest,
                recovery_baseline,
                recovery,
                recovery_baseline["deployment"],
            )
        self.assertEqual(
            set(recovered),
            {
                "startup_result",
                "preservation",
                "account_ready",
                "entry_ready",
                "log_chain_digest",
            },
        )
        foreign_deployment = copy.deepcopy(recovery_baseline["deployment"])
        foreign_deployment["db"] = {"foreign": True}
        with self.assertRaisesRegex(
            carry.CarryForwardError, "group2_recovery_evidence_invalid"
        ):
            carry.validate_group2_recovery_evidence(
                manifest, recovery_baseline, recovery, foreign_deployment
            )
        changed_baseline = copy.deepcopy(recovery_baseline)
        changed_baseline["predecessor"]["logs_after"] = recovery_before
        with (
            mock.patch.object(carry, "_validate_capture_digests"),
            self.assertRaisesRegex(
                carry.CarryForwardError, "group2_recovery_lineage_changed"
            ),
        ):
            carry.validate_group2_recovery_evidence(
                manifest,
                changed_baseline,
                recovery,
                recovery_baseline["deployment"],
            )

    def test_recovery_lineage_uses_only_deployment_or_prior_verified_after(self):
        manifest = group2_manifest()
        db = {
            "projection": manifest["old_runtime_projection"],
            "responsibilities": manifest["responsibilities"],
            "protected_rows": manifest["protected_rows"],
            "asset_projection": manifest["asset_projection"],
            "schema_shape": manifest["schema_shape"],
            "schema_inventory": manifest["schema_inventory"],
        }
        entries = [
            {
                "path": "old.bin",
                "type": "file",
                "mode": 0o600,
                "sha256": "a" * 64,
            }
        ]
        files = {
            "runtime": {"root": {}, "entries": entries, "digest": carry.digest(entries)},
            "journal": {"root": {}, "entries": [], "digest": carry.digest([])},
            "materials": {},
            "journal_facts": {"cleanup": [], "attempt": [], "sandbox_recovery": []},
            "empty_attempt_shells": [],
        }
        deployment = {
            "db": db,
            "files": files,
            "post_preservation": {"code": "group2_post_probe_ok"},
            "logs_after": manifest["log_evidence"],
        }
        predecessor = {
            "kind": "deployment",
            "recovery_id": None,
            "evidence_digest": None,
            "db": copy.deepcopy(db),
            "files": copy.deepcopy(files),
            "logs_after": manifest["log_evidence"],
        }
        fresh = {"db": copy.deepcopy(db), "files": copy.deepcopy(files)}
        carry._validate_group2_recovery_lineage(deployment, predecessor, fresh)
        for changed in (
            ("db", lambda value: value["responsibilities"].update(changed=True)),
            ("files", lambda value: value["runtime"]["entries"][0].update(mode=0o644)),
            ("files", lambda value: value["runtime"]["entries"].append({"path": "new"})),
        ):
            invalid = copy.deepcopy(fresh)
            changed[1](invalid[changed[0]])
            if changed[0] == "files":
                invalid["files"]["runtime"]["entries"].sort(key=lambda item: item["path"])
                invalid["files"]["runtime"]["digest"] = carry.digest(
                    invalid["files"]["runtime"]["entries"]
                )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "group2_recovery_lineage_changed"
            ):
                carry._validate_group2_recovery_lineage(
                    deployment, predecessor, invalid
                )
        prior_after = copy.deepcopy(predecessor)
        prior_after.update(
            kind="recovery", recovery_id="1" * 32, evidence_digest="2" * 64
        )
        prior_after["files"]["runtime"]["root"] = {"mtime_ns": 2}
        carry._validate_group2_recovery_lineage(
            deployment,
            prior_after,
            {"db": prior_after["db"], "files": prior_after["files"]},
        )

    def test_compose_uses_formal_overlay_and_loopback_parser_supports_ipv6(self):
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory)
            (release / "docker-compose.yml").write_text("services: {}\n")
            (release / "compose.preview.json").write_text('{"services":{}}\n')
            env = release / "preview.env"
            env.write_text(
                "DLR_WEB_HOST_PORT=[::1]:8080\n"
                "DLR_ACCOUNT_WEB_HOST_PORT=127.0.0.1:8081\n"
            )
            env.chmod(0o600)
            seen = []
            with mock.patch.object(
                carry,
                "_run_json",
                side_effect=lambda args, code: seen.append(args) or {},
            ):
                carry._compose_json("dlr", release, env)
            self.assertEqual(seen[0].count("-f"), 2)
            self.assertIn(str(release / "compose.preview.json"), seen[0])
            self.assertEqual(carry._loopback_binding("[::1]:8080"), ("::1", "8080"))
            for invalid in ("8080", "0.0.0.0:8080", "203.0.113.8:8080"):
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "account_binding_invalid"
                ):
                    carry._loopback_binding(invalid)
        self.assertIsNone(carry._command_part(None))
        self.assertEqual(carry._command_part("single command"), ["single command"])
        self.assertEqual(carry._command_part(["one", "two"]), ["one", "two"])
        config = {"services": {"worker": {"entrypoint": None, "command": ["override"]}}}
        self.assertEqual(
            carry._effective_command(config, "worker", [["entry"], ["default"]]),
            [["entry"], ["override"]],
        )
        with (
            mock.patch.object(
                carry,
                "_run_json",
                return_value=[
                    {
                        "Config": {
                            "Entrypoint": None,
                            "Cmd": ["run"],
                            "Volumes": {"/unexpected": {}},
                        }
                    }
                ],
            ),
            self.assertRaisesRegex(
                carry.CarryForwardError, "account_image_config_invalid"
            ),
        ):
            carry._image_command("sha256:image")
        with self.assertRaisesRegex(
            carry.CarryForwardError, "group2_runtime_request_invalid"
        ):
            carry.group2_runtime(
                {
                    "mode": carry.GROUP2_MODE,
                    "operation": "log-capture",
                    "profile": {},
                    "unknown": True,
                }
            )

    def test_probe_log_chain_is_unique_and_cleanup_follows_delete(self):
        lines = [
            'x 127.0.0.1:1001 - "POST /api/adapters HTTP/1.1" 201',
            'x 127.0.0.1:1002 - "POST /api/adapters/77/versions HTTP/1.1" 201',
            'x 127.0.0.1:1003 - "PATCH /api/adapters/77 HTTP/1.1" 200',
            'x 127.0.0.1:1004 - "POST /api/adapters/77/executions HTTP/1.1" 202',
            'x 127.0.0.1:1005 - "GET /api/executions/78 HTTP/1.1" 200',
            'x 172.18.0.6:1006 - "POST /api/workers/88/v3/claim HTTP/1.1" 200',
            'x 172.18.0.6:1007 - "POST /api/workers/88/attempts/99/start HTTP/1.1" 200',
            'x 172.18.0.6:1008 - "POST /api/workers/88/attempts/99/result HTTP/1.1" 200',
            'x 172.18.0.6:1009 - "POST /api/workers/executions/78/workspace-cleanup HTTP/1.1" 200',
            'x 127.0.0.1:1010 - "GET /api/executions/78 HTTP/1.1" 200',
            'x 127.0.0.1:1011 - "DELETE /api/adapters/77 HTTP/1.1" 204',
        ]
        before_logs, after_logs = self._log_window("\n".join(lines), size=123)
        before_db = {
            "protected_rows": {
                "adapter_ids": [1],
                "execution_ids": [2],
                "attempt_ids": [3],
            }
        }
        request = {
            "mode": carry.GROUP2_MODE,
            "operation": "probe-proof",
            "logs_before": before_logs,
            "logs_after": after_logs,
            "probe_result": {
                "execution_id": 78,
                "status": "succeeded",
                "workspace_cleanup_status": "completed",
            },
            "before_db": before_db,
            "cleanup": None,
        }
        partial = carry.derive_group2_probe_provenance(request)
        self.assertEqual(partial["attempt_id"], 99)
        self.assertEqual(partial["event_refs"][0]["offset"], 123)
        cleanup_row = {"id": 66, "adapter_id": 77, "worker_id": 88}
        cleanup = {"row": cleanup_row, "row_sha256": carry.digest(cleanup_row)}
        lines += [
            'x 172.18.0.6:1012 - "POST /api/workers/88/cleanups/claim HTTP/1.1" 200',
            'x 172.18.0.6:1013 - "POST /api/workers/88/cleanups/66/result HTTP/1.1" 204',
        ]
        request["logs_before"], request["logs_after"] = self._log_window(
            "\n".join(lines), size=123
        )
        request["cleanup"] = cleanup
        final = carry.derive_group2_probe_provenance(request)
        self.assertEqual(final["cleanup"], cleanup)
        request["cleanup"] = None
        self.assertIsNone(carry.derive_group2_probe_provenance(request)["cleanup"])
        request["cleanup"] = cleanup
        self._replace_log_text(
            request["logs_after"],
            "\n".join(
                [*lines, 'x 127.0.0.1:1014 - "POST /api/users HTTP/1.1" 201']
            ),
        )
        with self.assertRaisesRegex(
            carry.CarryForwardError, "probe_business_write_unknown"
        ):
            carry.derive_group2_probe_provenance(request)

    def test_probe_rejects_nonloopback_business_extra_claim_and_cleanup(self):
        oracle = """t INFO access 127.0.0.1:1001 - "POST /api/adapters HTTP/1.1" 201
t INFO access 127.0.0.1:1002 - "POST /api/adapters/14/versions HTTP/1.1" 201
t INFO access 127.0.0.1:1003 - "PATCH /api/adapters/14 HTTP/1.1" 200
t INFO access 127.0.0.1:1004 - "POST /api/adapters/14/executions HTTP/1.1" 202
t INFO access 127.0.0.1:1005 - "GET /api/executions/48 HTTP/1.1" 200
t INFO access 172.18.0.6:1006 - "POST /api/workers/1/v3/claim HTTP/1.1" 200
t INFO access 172.18.0.6:1007 - "POST /api/workers/1/attempts/47/start HTTP/1.1" 200
t INFO access 172.18.0.6:1008 - "POST /api/workers/1/attempts/47/result HTTP/1.1" 200
t INFO access 172.18.0.6:1009 - "POST /api/workers/executions/48/workspace-cleanup HTTP/1.1" 200
t INFO access 127.0.0.1:1010 - "GET /api/executions/48 HTTP/1.1" 200
t INFO access 127.0.0.1:1011 - "DELETE /api/adapters/14 HTTP/1.1" 204
t INFO access 172.18.0.6:1012 - "POST /api/workers/1/cleanups/claim HTTP/1.1" 200
t INFO access 172.18.0.6:1013 - "POST /api/workers/1/cleanups/24/result HTTP/1.1" 204"""
        before_logs, after_logs = self._log_window(oracle)
        request = {
            "mode": carry.GROUP2_MODE,
            "operation": "probe-proof",
            "logs_before": before_logs,
            "logs_after": after_logs,
            "probe_result": {
                "execution_id": 48,
                "status": "succeeded",
                "workspace_cleanup_status": "completed",
            },
            "before_db": {
                "protected_rows": {
                    "adapter_ids": [],
                    "execution_ids": [],
                    "attempt_ids": [],
                }
            },
            "cleanup": None,
        }
        self.assertEqual(
            carry.derive_group2_probe_provenance(request)["adapter_id"], 14
        )
        for needle, replacement, code in (
            ("127.0.0.1:", "203.0.113.8:", "probe_business_not_loopback"),
            (
                "t INFO access 172.18.0.6:1012",
                'x 172.18.0.6:6000 - "POST /api/workers/1/v3/claim HTTP/1.1" 200\n'
                + "t INFO access 172.18.0.6:1012",
                "probe_claim_chain_invalid",
            ),
            (
                "t INFO access 172.18.0.6:1012",
                'x 172.18.0.6:6000 - "POST /api/workers/1/cleanups/999/result HTTP/1.1" 204\n'
                + "t INFO access 172.18.0.6:1012",
                "probe_cleanup_log_invalid",
            ),
        ):
            changed = copy.deepcopy(request)
            self._replace_log_text(
                changed["logs_after"], oracle.replace(needle, replacement, 1)
            )
            with self.assertRaisesRegex(carry.CarryForwardError, code):
                carry.derive_group2_probe_provenance(changed)
        self._replace_log_text(
            request["logs_after"],
            "\n".join(
                line
                for line in oracle.splitlines()
                if "DELETE /api/adapters" not in line
            ),
        )
        with self.assertRaisesRegex(
            carry.CarryForwardError, "probe_provenance_invalid"
        ):
            carry.derive_group2_probe_provenance(request)

    def test_account_capture_binds_old_and_candidate_service_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            env_file = root / "preview.env"
            env_file.write_text(
                "DLR_WEB_HOST_PORT=127.0.0.1:8080\n"
                "DLR_ACCOUNT_WEB_HOST_PORT=127.0.0.1:8081\n"
            )
            env_file.chmod(0o600)

            def config(sha):
                services = {}
                for service in ("control", "worker", "web", "account-web"):
                    image_service = "web" if service == "account-web" else service
                    services[service] = {
                        "image": f"project-{image_service}:{sha}",
                        "command": [service],
                        "networks": {"default": {}},
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(root / service),
                                "target": f"/var/lib/dlr/platform-logs/{service}",
                                "read_only": False,
                            }
                        ],
                        "ports": (
                            [
                                {
                                    "host_ip": "127.0.0.1",
                                    "published": 8081,
                                    "target": 80,
                                    "protocol": "tcp",
                                }
                            ]
                            if service == "account-web"
                            else [
                                {
                                    "host_ip": "127.0.0.1",
                                    "published": 8080,
                                    "target": 80,
                                    "protocol": "tcp",
                                }
                            ]
                            if service == "web"
                            else []
                        ),
                    }
                return {
                    "services": services,
                    "networks": {"default": {"name": "project_default"}},
                }

            old_images = {
                f"project-{service}:{SHA_A}": f"old-{service}"
                for service in ("control", "worker", "web")
            }
            candidate_images = {
                f"project-{service}:{SHA_B}": f"new-{service}"
                for service in ("control", "worker", "web")
            }

            def inspected(_project, service):
                image_service = "web" if service == "account-web" else service
                return {
                    "container_id": "old-" + service,
                    "image_id": old_images.get(
                        f"project-{image_service}:{SHA_A}", "older-account"
                    ),
                    "status": "exited",
                    "health": None,
                    "started_at": "old",
                    "restart_count": 0,
                    "command": [None, [service]],
                    "labels": {
                        "com.docker.compose.project": "project",
                        "com.docker.compose.service": service,
                    },
                    "port_bindings": (
                        {
                            "80/tcp": [
                                {
                                    "HostIp": "127.0.0.1",
                                    "HostPort": "8081"
                                    if service == "account-web"
                                    else "8080",
                                }
                            ]
                        }
                        if service in {"web", "account-web"}
                        else {}
                    ),
                    "mounts": [
                        {
                            "type": "bind",
                            "source": str(root / service),
                            "destination": f"/var/lib/dlr/platform-logs/{service}",
                            "rw": True,
                        }
                    ],
                    "networks": ["project_default"],
                }

            request = {
                "mode": carry.GROUP2_MODE,
                "operation": "account-capture",
                "project": "project",
                "from_sha": SHA_A,
                "to_sha": SHA_B,
                "old_release": str(root / "old"),
                "candidate_release": str(root / "new"),
                "env_file": str(env_file),
                "old_image_ids": old_images,
                "candidate_image_ids": candidate_images,
            }
            with (
                mock.patch.object(
                    carry, "_compose_json", side_effect=[config(SHA_A), config(SHA_B)]
                ),
                mock.patch.object(
                    carry, "_image_command", return_value=[None, ["image-default"]]
                ),
                mock.patch.object(carry, "_inspect_container", side_effect=inspected),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                profile = carry.capture_group2_account_entry(request)
            self.assertEqual(profile["candidate_web_image_id"], "new-web")
            self.assertEqual(
                profile["candidate_profiles"]["web"]["networks"],
                ["project_default"],
            )

            def wrong_inspected(project, service):
                value = inspected(project, service)
                if service == "worker":
                    value["image_id"] = "wrong"
                return value

            with (
                mock.patch.object(
                    carry, "_compose_json", side_effect=[config(SHA_A), config(SHA_B)]
                ),
                mock.patch.object(
                    carry, "_image_command", return_value=[None, ["image-default"]]
                ),
                mock.patch.object(
                    carry, "_inspect_container", side_effect=wrong_inspected
                ),
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "account_image_invalid"
                ),
            ):
                carry.capture_group2_account_entry(request)

            def wrong_binding(project, service):
                value = inspected(project, service)
                if service == "account-web":
                    value["labels"] = {
                        **value["labels"],
                        "com.docker.compose.project": "other",
                    }
                return value

            with (
                mock.patch.object(
                    carry, "_compose_json", side_effect=[config(SHA_A), config(SHA_B)]
                ),
                mock.patch.object(
                    carry, "_image_command", return_value=[None, ["image-default"]]
                ),
                mock.patch.object(
                    carry, "_inspect_container", side_effect=wrong_binding
                ),
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "account_old_binding_invalid"
                ),
            ):
                carry.capture_group2_account_entry(request)

    def test_entry_probe_covers_token_csrf_and_spoof_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            env_file = root / "preview.env"
            env_file.write_text(
                "DLR_WEB_HOST_PORT=127.0.0.1:8080\n"
                "DLR_ACCOUNT_WEB_HOST_PORT=127.0.0.1:8081\n"
                "DLR_ADMIN_TOKEN=private-test-token\n"
            )
            env_file.chmod(0o600)
            profile = self._profile(root / "worker.log")
            base = {
                "status": 401,
                "code": "unauthorized",
                "body_status": None,
                "csrf_cookie": False,
                "csrf_cookie_path": False,
                "csrf_cookie_samesite_lax": False,
                "csrf_cookie_httponly": False,
                "redirect": False,
            }

            def response(url, method="GET", headers=None):
                headers = headers or {}
                if url.endswith("/api/auth/account/csrf"):
                    if ":8081" in url:
                        return {
                            **base,
                            "status": 200,
                            "code": None,
                            "body_status": "ok",
                            "csrf_cookie": True,
                            "csrf_cookie_path": True,
                            "csrf_cookie_samesite_lax": True,
                        }
                    return {**base, "code": "account_entry_required"}
                if url.endswith("/api/auth/admin/verify"):
                    if ":8081" in url:
                        return {**base, "code": "token_entry_required"}
                    if headers.get("Authorization") == "Bearer private-test-token":
                        return {
                            **base,
                            "status": 200,
                            "code": None,
                            "body_status": "ok",
                        }
                if url.endswith("/api/auth/account/me"):
                    return {**base, "code": "account_session_required"}
                if url.endswith("/api/auth/account/logout"):
                    return {**base, "status": 403, "code": "account_csrf_invalid"}
                return base

            request = {
                "mode": carry.GROUP2_MODE,
                "operation": "entry-probe",
                "profile": profile,
                "env_file": str(env_file),
            }
            with (
                mock.patch.object(carry, "_http_result", side_effect=response),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                result = carry._entry_probe(request)
            self.assertTrue(result["passed"])
            self.assertEqual(carry.validate_group2_entry_probe(profile, result), result)
            changed = copy.deepcopy(result)
            changed["probes"]["account_write_bad_csrf"]["status"] = 200
            with self.assertRaisesRegex(
                carry.CarryForwardError, "entry_boundary_invalid"
            ):
                carry.validate_group2_entry_probe(profile, changed)

            def redirected(url, method="GET", headers=None):
                value = response(url, method, headers)
                if url.endswith("/api/auth/account/csrf") and ":8081" in url:
                    value = {**value, "redirect": True}
                return value

            with (
                mock.patch.object(carry, "_http_result", side_effect=redirected),
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "entry_boundary_invalid"
                ),
            ):
                carry._entry_probe(request)

    def test_account_check_closes_all_bindings_and_real_account_csrf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker" / "worker.log"
            path.parent.mkdir()
            profile = self._profile(path)

            def inspected(_project, service):
                service_profile = profile["candidate_profiles"][service]
                return {
                    "container_id": "new-" + service,
                    "image_id": profile["candidate_image_ids_by_service"][service],
                    "status": "running",
                    "health": "healthy",
                    "started_at": "2026-09-20T00:00:00Z",
                    "restart_count": 0,
                    "command": [None, service_profile["command"]],
                    "labels": {
                        "com.docker.compose.project": "dlr",
                        "com.docker.compose.service": service,
                    },
                    "port_bindings": carry._profile_port_bindings(service_profile),
                    "mounts": service_profile["mounts"],
                    "networks": service_profile["networks"],
                }

            ok = {
                "status": 200,
                "code": None,
                "body_status": "ok",
                "csrf_cookie": True,
                "csrf_cookie_path": True,
                "csrf_cookie_samesite_lax": True,
                "csrf_cookie_httponly": False,
                "redirect": False,
            }
            request = {
                "mode": carry.GROUP2_MODE,
                "operation": "account-check",
                "profile": profile,
                "project": "dlr",
                "to_sha": SHA_B,
                "candidate_image_ids": {
                    f"dlr-{service}:{SHA_B}": "sha256:" + "b" * 64
                    for service in ("control", "worker", "web")
                },
            }
            with (
                mock.patch.object(carry, "_inspect_container", side_effect=inspected),
                mock.patch.object(carry, "_http_result", return_value=ok) as http,
            ):
                result = carry.check_group2_entry_boundaries(request)
            self.assertEqual(result["account_csrf"], ok)
            self.assertIn("127.0.0.1:8081", http.call_args.args[0])
            self.assertEqual(
                carry.validate_group2_account_check(profile, result), result
            )
            changed_result = copy.deepcopy(result)
            changed_result["containers"]["worker"]["status"] = "exited"
            with self.assertRaisesRegex(
                carry.CarryForwardError, "account_binding_changed"
            ):
                carry.validate_group2_account_check(profile, changed_result)

            for service, field, changed in (
                ("web", "command", [None, ["changed"]]),
                ("control", "port_bindings", {"8000/tcp": []}),
                ("account-web", "networks", ["other"]),
            ):

                def wrong(
                    _project, current, *, target=service, key=field, value=changed
                ):
                    item = inspected(_project, current)
                    if current == target:
                        item[key] = value
                    return item

                with (
                    self.subTest(service=service, field=field),
                    mock.patch.object(carry, "_inspect_container", side_effect=wrong),
                    mock.patch.object(carry, "_http_result", return_value=ok),
                    self.assertRaisesRegex(
                        carry.CarryForwardError, "account_binding_changed"
                    ),
                ):
                    carry.check_group2_entry_boundaries(request)

            unhealthy = {**ok, "csrf_cookie_samesite_lax": False}
            with (
                mock.patch.object(carry, "_inspect_container", side_effect=inspected),
                mock.patch.object(carry, "_http_result", return_value=unhealthy),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "account_entry_unhealthy"
                ),
            ):
                carry.check_group2_entry_boundaries(request)

    def test_startup_proof_binds_log_image_window_and_full_capabilities(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "worker" / "worker.log"
            worker.parent.mkdir()
            worker.touch()
            profile = self._profile(worker)
            nonce = "1" * 32
            capabilities = {
                name: True
                for name in (
                    "adapter_control_plane_hidden",
                    "adapter_mount_blocked",
                    "bounded_output",
                    "cgroup_kill",
                    "cgroup_namespace_private",
                    "cgroup_v2",
                    "cpu_hard_limit",
                    "memory_hard_limit",
                    "mount_namespace",
                    "no_new_privileges",
                    "nofile_hard_limit",
                    "pid_namespace",
                    "pids_hard_limit",
                    "preflight_passed",
                    "sandbox_cleanup",
                    "swap_hard_limit",
                    "tmpfs_hard_limit",
                )
            }
            receipt = {
                "cgroup_name": f"dlr-preflight-{nonce}",
                "status": "passed",
                "workspace_residue": False,
                "capabilities": capabilities,
                "adapter_control_pipe_fds": [],
                "adapter_hidden_cgroup_paths": {
                    path: {"read_blocked": True, "write_blocked": True}
                    for path in ("/run/dlr-cgroup", "/sys/fs/cgroup")
                },
                "agent_outside_attempt": True,
                "helper_outside_attempt": True,
                "probe_in_attempt": True,
                "child_empty_after_kill": True,
                "process_exited_after_kill": True,
                "worker_cgroup_management": {
                    "child_limit_write_read": True,
                    "parent_controllers_read": True,
                },
                "namespace_identity": {
                    "boot_id": "00000000-0000-4000-8000-000000000001",
                    "parent_device": 30,
                    "parent_inode": 51,
                    "root_device": 30,
                    "root_inode": 197,
                },
                "cleanup": {
                    "cgroup_name": f"dlr-preflight-{nonce}",
                    "error_code": None,
                    "residue": False,
                    "status": "completed",
                },
                "error_code": "resource_exceeded_disk",
                "helper_diagnostic": {
                    "errno": 28,
                    "error_code": "resource_exceeded_disk",
                },
            }
            before = carry.capture_log_prefix(profile)
            worker.write_text(
                "sandbox preflight receipt: "
                + json.dumps(receipt, sort_keys=True)
                + "\nsandbox preflight passed; rabbitmq execution gate=True\n"
            )
            after = carry.read_log_append(before)
            service_profile = profile["candidate_profiles"]["worker"]
            container_after = {
                "container_id": "new-worker",
                "image_id": profile["candidate_image_ids_by_service"]["worker"],
                "status": "running",
                "health": "healthy",
                "started_at": "2026-09-20T00:00:01.123456789Z",
                "restart_count": 0,
                "command": [None, service_profile["command"]],
                "labels": {
                    "com.docker.compose.project": "dlr",
                    "com.docker.compose.service": "worker",
                },
                "port_bindings": {},
                "mounts": service_profile["mounts"],
                "networks": service_profile["networks"],
            }
            start = 1_789_862_401_000_000_000
            end = start + 999_999_999
            before["observed_at_ns"] = start
            before["clock"] = precise_log_clock(start)
            before["evidence_digest"] = carry.digest(
                {key: item for key, item in before.items() if key != "evidence_digest"}
            )
            after["baseline_evidence_digest"] = before["evidence_digest"]
            after["observed_after_ns"] = end
            after["clock"] = precise_log_clock(end)
            after["evidence_digest"] = carry.digest(
                {key: item for key, item in after.items() if key != "evidence_digest"}
            )
            request = {
                "mode": carry.GROUP2_MODE,
                "operation": "startup-proof",
                "profile": profile,
                "logs_before": before,
                "logs_after": after,
                "container_before": {"container_id": "old-worker"},
                "container_after": container_after,
                "window_start_ns": start,
                "window_end_ns": end,
            }
            self.assertEqual(carry._startup_proof(request)["nonce"], nonce)
            for mutate, code in (
                (
                    lambda value: value["container_after"].__setitem__(
                        "image_id", "wrong-image"
                    ),
                    "startup_proof_invalid",
                ),
                (
                    lambda value: value["container_after"].__setitem__(
                        "started_at", "1900-01-01T00:00:00Z"
                    ),
                    "startup_proof_invalid",
                ),
                (
                    lambda value: value["logs_after"].__setitem__(
                        "baseline_evidence_digest", "f" * 64
                    ),
                    "startup_log_evidence_invalid",
                ),
            ):
                changed = copy.deepcopy(request)
                mutate(changed)
                with self.assertRaisesRegex(carry.CarryForwardError, code):
                    carry._startup_proof(changed)
            changed = copy.deepcopy(request)
            log = changed["logs_after"]["files"]
            worker_item = next(item for item in log if item["path"] == str(worker))
            worker_item["appended_text"] = worker_item["appended_text"].replace(
                '"preflight_passed": true', '"preflight_passed": false'
            )
            worker_item["end_size"] = len(worker_item["appended_text"].encode())
            worker_item["end_sha256"] = hashlib.sha256(
                worker_item["appended_text"].encode()
            ).hexdigest()
            changed["logs_after"]["evidence_digest"] = carry.digest(
                {
                    key: item
                    for key, item in changed["logs_after"].items()
                    if key != "evidence_digest"
                }
            )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "startup_proof_invalid"
            ):
                carry._startup_proof(changed)

            for field, timestamp in (
                ("observed_at_ns", start + 1),
                ("observed_after_ns", end - 1),
            ):
                changed = copy.deepcopy(request)
                target = (
                    changed["logs_before"]
                    if field == "observed_at_ns"
                    else changed["logs_after"]
                )
                target[field] = timestamp
                target["clock"] = precise_log_clock(timestamp)
                if field == "observed_at_ns":
                    target["evidence_digest"] = carry.digest(
                        {
                            key: item
                            for key, item in target.items()
                            if key != "evidence_digest"
                        }
                    )
                    changed["logs_after"]["baseline_evidence_digest"] = target[
                        "evidence_digest"
                    ]
                changed["logs_after"]["evidence_digest"] = carry.digest(
                    {
                        key: item
                        for key, item in changed["logs_after"].items()
                        if key != "evidence_digest"
                    }
                )
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "startup_proof_invalid"
                ):
                    carry._startup_proof(changed)


if __name__ == "__main__":
    unittest.main()
