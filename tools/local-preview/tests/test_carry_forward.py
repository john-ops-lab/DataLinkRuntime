import copy
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
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
    log_root_evidence = [
        {
            "path": item["path"],
            "device": 1,
            "inode": index,
            "mode": 0o700,
            "uid": 2,
            "gid": 3,
            "mtime_ns": 0,
            "entries": [],
            "allowed_new_files": sorted(item["allowed_new_files"]),
        }
        for index, item in enumerate(profile["log_roots"], 10)
    ]
    logs = {
        "profile_digest": profile["profile_digest"],
        "files": [
            {"path": path, "exists": False} for path in profile["log_files"]
        ],
        "roots": log_root_evidence,
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
        for mutation in ("empty", "other-root", "missing-required-file"):
            changed = copy.deepcopy(manifest)
            logs = changed["log_evidence"]
            if mutation == "empty":
                logs["roots"] = []
            elif mutation == "other-root":
                for root in logs["roots"]:
                    root["path"] = root["path"].replace(
                        "/private/logs", "/private/other-logs"
                    )
                for item in logs["files"]:
                    item["path"] = item["path"].replace(
                        "/private/logs", "/private/other-logs"
                    )
            else:
                logs["files"].pop()
            logs["evidence_digest"] = carry.digest(
                {key: item for key, item in logs.items() if key != "evidence_digest"}
            )
            changed["manifest_digest"] = carry.digest(carry.manifest_payload(changed))
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(carry.CarryForwardError, "log_evidence_invalid"),
            ):
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

    def test_reconcile_capture_uses_private_ids_without_historical_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            evidence = directory / "evidence"
            evidence.mkdir(mode=0o700)
            manifest = {
                "selection": {
                    "queued": [{"execution_id": 7, "incident_ids": [11]}],
                    "cleanup_execution_ids": [],
                },
                "storage_identity": [
                    {
                        "service": "worker",
                        "type": "volume",
                        "source": "runtime-volume",
                        "destination": "/var/lib/dlr/runtime",
                    },
                    {
                        "service": "worker",
                        "type": "volume",
                        "source": "journal-volume",
                        "destination": "/var/lib/dlr/journal",
                    },
                    {
                        "service": "control",
                        "type": "volume",
                        "source": "builtin-volume",
                        "destination": "/var/lib/dlr/builtin-packages",
                    },
                    {
                        "service": "control",
                        "type": "volume",
                        "source": "artifact-volume",
                        "destination": "/var/lib/dlr/artifacts",
                    },
                ],
                "old_image_ids": {
                    f"example-control:{carry.GROUP2_FROM_SHA}": "sha256:old-control"
                },
            }
            docker_arguments = None

            def checked_output(arguments):
                if arguments[1:3] == ["inspect", "preview-worker-1"]:
                    return "1000:1000"
                if arguments[1:3] == ["inspect", "preview-control-1"]:
                    return json.dumps(
                        [
                            {
                                "Config": {
                                    "Env": [
                                        "DATABASE_URL=postgresql://synthetic",
                                        "PGOPTIONS=-c default_transaction_read_only=on",
                                        "IGNORED=value",
                                    ]
                                },
                                "NetworkSettings": {"Networks": {"preview": {}}},
                            }
                        ]
                    )
                self.fail(f"unexpected command: {arguments!r}")

            def run(arguments, *, check, timeout):
                nonlocal docker_arguments
                docker_arguments = arguments
                self.assertTrue(check)
                self.assertEqual(timeout, 300)
                output = directory / "preflight"
                carry.write_private(output / "db.json", {"db": "captured"})
                carry.write_private(output / "files.json", {"files": "captured"})

            with (
                mock.patch.object(carry, "_checked_output", side_effect=checked_output),
                mock.patch.object(carry.subprocess, "run", side_effect=run),
            ):
                result = carry._capture_reconcile_state(
                    directory, directory, "preflight", manifest, "preview"
                )

            self.assertEqual(result, ({"db": "captured"}, {"files": "captured"}))
            self.assertIsNotNone(docker_arguments)
            self.assertEqual(
                carry.read_private(directory / "preflight" / "ids.json"),
                manifest["selection"],
            )
            self.assertIn(
                f"type=bind,source={directory / 'preflight'},target=/evidence",
                docker_arguments,
            )
            self.assertFalse(
                any("target=/baseline.json" in item for item in docker_arguments)
            )
            self.assertNotIn("--baseline", docker_arguments)
            ids_index = docker_arguments.index("--ids")
            self.assertEqual(
                docker_arguments[ids_index + 1], "/evidence/ids.json"
            )

            container_root = directory / "container-root"
            container_root.mkdir(mode=0o755)
            container_evidence = container_root / "evidence"
            container_evidence.mkdir(mode=0o700)
            new_target = container_evidence / "ids.json"
            carry.write_private(new_target, manifest["selection"])
            self.assertEqual(
                carry.read_private(new_target), manifest["selection"]
            )
            old_target = container_root / "baseline.json"
            old_target.write_text('{"manifest":"baseline"}\n')
            old_target.chmod(0o600)
            with self.assertRaisesRegex(
                carry.CarryForwardError, "private_parent_invalid"
            ):
                carry.read_private(old_target)

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
    def _reboot_request(self):
        prepare_raw = (Path(__file__).parents[3] / "scripts" / "prepare-sandbox-host.sh").read_bytes()
        containers = {
            name: {"container_id": name, "image_id": "sha256:" + name,
                   "inspect_static_digest": carry.digest({
                       "Id": name, "Image": "sha256:" + name, "Name": "/" + name,
                       "Config": {}, "HostConfig": {}, "Mounts": [],
                   }),
                   "stopped_state_digest": carry.digest({"Status": "exited"})}
            for name in ("postgres", "rabbitmq", "control", "worker", "web", "account-web")
        }
        prior_success = {
            "schema": "group2-prior-success-evidence-v1", "host": {}, "vm": {}
        }
        authority_paths = carry.group2_post_finalize_reboot_expected_authority_paths(
            prior_success
        )
        descriptor = {
            "exists": True, "kind": "regular", "sha256": "a" * 64,
            "mode": 0o600, "uid": os.geteuid(), "gid": os.getegid(),
            "symlink": False,
        }
        prior = {
            "sha": carry.GROUP2_FROM_SHA,
            "schema": "0040_issue152_dispositions",
            "host_files": {
                name: ({"exists": False} if name == "attention.json"
                       else copy.deepcopy(descriptor))
                for name in authority_paths["host"]
            },
            "vm_files": {
                name: copy.deepcopy(descriptor) for name in authority_paths["vm"]
            },
            "installed": {
                "host": {name: copy.deepcopy(descriptor)
                         for name in carry.GROUP2_CONTROLLER_FILES},
                "vm": {name: copy.deepcopy(descriptor)
                       for name in carry.GROUP2_POST_FINALIZE_REBOOT_VM_INSTALLED},
            },
            "containers": containers,
            "images": {name: value["image_id"] for name, value in containers.items()},
            "storage": [],
            "parameters": {
                "keeper": {"unit": "dlr.service", "cpu_quota": "200%",
                           "memory_max": "2G"},
                "rabbitmq": {"queues": [{"vhost": "/", "name": "dispatch"}]},
            },
        }
        for side in ("host_files", "vm_files"):
            prior[side]["prepare-sandbox-host.sh"]["sha256"] = hashlib.sha256(
                prepare_raw
            ).hexdigest()
            prior[side]["prepare-sandbox-host.sh"]["mode"] = 0o755
        parent_chain = {
            "request": {"incident_id": carry.GROUP2_PARTIAL_INCIDENT_ID},
            "result": {"receipt": {"ok": True}},
        }
        snapshot = {"selection": {}, "db": {}, "files": {}, "lineage": []}
        review = {"status": "APPROVED"}
        artifacts_values = {
            "parent-finalize.json": {
                "chain": parent_chain,
                "original_reference": {},
                "snapshot": snapshot,
                "review": review,
            },
            "stopped-platform.json": {
                "schema": "group2-post-finalize-reboot-platform-v1",
                "prior": prior,
                "account_entry": {"profile": "frozen"},
                "prior_nonces": ["1" * 32],
                "prepare_script": {
                    "sha256": hashlib.sha256(prepare_raw).hexdigest(),
                    "content_b64": base64.b64encode(prepare_raw).decode(),
                    "source_mode": "100755",
                    "host": copy.deepcopy(
                        prior["host_files"]["prepare-sandbox-host.sh"]
                    ),
                    "vm": copy.deepcopy(
                        prior["vm_files"]["prepare-sandbox-host.sh"]
                    ),
                    "parameters": prior["parameters"]["keeper"],
                },
            },
            "prior-success.json": prior_success,
            "source-review.json": {
                "schema": "group2-post-finalize-reboot-source-review-v1",
                "source": {
                    "source_scope": {"to_tree": "e" * 40},
                    "tool_review": {"sha256": "f" * 64},
                },
                "ci": {"raw": "ci"},
            },
            "scope-approval.json": {
                "schema": "group2-post-finalize-reboot-scope-approval-v1",
                "status": "APPROVED",
                "execution": False,
                "request_scope": "implementation-review-ci-only",
            },
        }
        artifacts = {
            name: carry.canonical_bytes(value) for name, value in artifacts_values.items()
        }
        tool = {
            "sha": "1" * 40,
            "tree": "e" * 40,
            "controller_files": {name: "2" * 64 for name in carry.GROUP2_CONTROLLER_FILES},
            "source_scope_digest": "3" * 64,
            "review_report_sha256": "f" * 64,
            "ci_evidence_sha256": carry.digest(artifacts_values["source-review.json"]["ci"]),
        }
        request = {
            "schema": "group2-post-finalize-reboot-request-v1",
            "incident_id": carry.GROUP2_PARTIAL_INCIDENT_ID,
            "finalize_id": carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID,
            "boot_id": "00000000-0000-4000-8000-000000000001",
            "parent": {
                "request_sha256": carry.digest(parent_chain["request"]),
                "receipt_sha256": carry.digest(parent_chain["result"]["receipt"]),
                "chain_sha256": carry.digest(parent_chain),
                "snapshot_sha256": carry.digest(snapshot),
                "review_sha256": carry.digest(review),
            },
            "prior": prior,
            "tool": tool,
            "window": {"not_before_ns": 100, "deadline_ns": 200,
                       "successor_deadline_ns": 300},
            "evidence_files": {
                name: hashlib.sha256(raw).hexdigest() for name, raw in artifacts.items()
            },
        }
        request["request_digest"] = carry.digest(request)
        actions = carry.GROUP2_POST_FINALIZE_REBOOT_ACTIONS
        user = {
            "schema": "group2-post-finalize-reboot-user-record-v1",
            "request_digest": request["request_digest"],
            "tool_sha": tool["sha"],
            "boot_id": request["boot_id"],
            "actions": actions,
            "presented_request": " ".join(
                (request["request_digest"], tool["sha"], request["boot_id"], *actions)
            ),
            "user_reply": "approve exact reboot",
        }
        user_raw = carry.canonical_bytes(user)
        approval = {
            "schema": "group2-post-finalize-reboot-approval-v1",
            "request_digest": request["request_digest"],
            "tool_sha": tool["sha"],
            "boot_id": request["boot_id"],
            "actions": actions,
            "user_record_sha256": hashlib.sha256(user_raw).hexdigest(),
        }
        return request, approval, user_raw, artifacts, snapshot, review

    def test_reboot_request_binds_raw_parent_source_window_and_prepare_script(self):
        request, approval, user, artifacts, snapshot, review = self._reboot_request()
        parent = {
            "snapshot": snapshot, "review": review,
            "evidence": {
                "current": {
                    "containers": {
                        name: carry._reboot_expected_container(value)
                        for name, value in request["prior"]["containers"].items()
                    },
                    "storage": request["prior"]["storage"],
                    "kernel": {
                        "unit": "dlr.service",
                        "description": (
                            "DataLinkRuntime Sandbox dlr.service CPU=200% Memory=2G"
                        ),
                    },
                },
                "authority": {"prior_success": {"host": {}, "vm": {}}},
            },
            "validated": {"context": {"originals": {
                "manifest": {"account_entry": artifacts and {"profile": "frozen"}},
                "first_startup": {"proof": {"nonce": "1" * 32}},
            }}},
            "receipt": {"startup_proof": {"nonce": "1" * 32}},
        }
        with (
            mock.patch.object(carry, "_post_finalize_reboot_parent", return_value=parent),
            mock.patch.object(carry, "_validate_group2_partial_tool"),
            mock.patch.object(carry, "validate_group2_account_entry",
                              side_effect=lambda value: value),
            mock.patch.object(carry, "_group2_partial_history",
                              return_value={"proof": {"nonce": "1" * 32}}),
        ):
            validated = carry.validate_group2_post_finalize_reboot_request(
                request, approval, user, artifacts
            )
            self.assertEqual(validated["parent"], parent)
            for mutate in (
                lambda value: value["window"].__setitem__("successor_deadline_ns", 200),
                lambda value: value["parent"].__setitem__("receipt_sha256", "0" * 64),
            ):
                changed = copy.deepcopy(request)
                mutate(changed)
                changed["request_digest"] = carry.digest(
                    {key: item for key, item in changed.items() if key != "request_digest"}
                )
                changed_approval = copy.deepcopy(approval)
                changed_approval["request_digest"] = changed["request_digest"]
                with self.assertRaises(carry.CarryForwardError):
                    carry.validate_group2_post_finalize_reboot_request(
                        changed, changed_approval, user, artifacts
                    )
            changed_artifacts = dict(artifacts)
            platform = json.loads(changed_artifacts["stopped-platform.json"])
            platform["prepare_script"]["source_mode"] = "100644"
            changed_artifacts["stopped-platform.json"] = carry.canonical_bytes(platform)
            with self.assertRaises(carry.CarryForwardError):
                carry.validate_group2_post_finalize_reboot_request(
                    request, approval, user, changed_artifacts
                )

    def test_reboot_transitions_are_ordered_and_start_only_same_ids(self):
        containers = {
            name: {
                "container_id": name + "-id",
                "image_id": name + "-image",
                "status": "exited",
                "health": None,
                "restart_count": 0,
                "inspect_static_digest": carry.digest({
                    "Id": name + "-id", "Image": name + "-image",
                    "Name": "/" + name, "Config": {}, "HostConfig": {}, "Mounts": [],
                }),
                "stopped_state_digest": carry.digest({"Status": "exited"}),
            }
            for name in ("postgres", "rabbitmq", "control", "worker", "web", "account-web")
        }
        prior = {
            "containers": copy.deepcopy(containers),
            "images": {name: item["image_id"] for name, item in containers.items()},
            "storage": [],
            "host_files": {}, "vm_files": {}, "installed": {},
            "parameters": {"rabbitmq": {"queues": [{"vhost": "/", "name": "q"}]}},
        }
        validated = {
            "request": {
                "boot_id": "00000000-0000-4000-8000-000000000001",
                "window": {"not_before_ns": 100, "deadline_ns": 900,
                           "successor_deadline_ns": 1000},
                "prior": prior,
            },
            "approval": {}, "user_record": {},
            "platform": {"account_entry": {"old_profiles": {"worker": {}},
                                             "profile_digest": "d" * 64},
                         "prior_nonces": []},
            "authority_paths": {"host": [], "vm": []},
            "prior_success": {
                "schema": "group2-prior-success-evidence-v1", "host": {}, "vm": {}
            },
            "parent": {"snapshot": {"db": {"rows": "same"},
                                    "files": {"tree": "same"}},
                       "evidence": {
                           "current": {"logs": {"parent": True},
                                       "kernel": {"boot_id": "old-boot"}},
                           "postgres": {"schema": "0040_issue152_dispositions",
                                        "binary_version": "16", "server_version": "16",
                                        "data_pg_version": "17"},
                       }},
        }
        stopped = {
            "schema": "group2-post-finalize-reboot-stopped-v1",
            "boot_id": validated["request"]["boot_id"],
            "files": {"tree": "same"}, "logs": {"prefix": "same"},
            "containers": {
                name: carry._reboot_expected_container(item)
                for name, item in containers.items()
            }, "images": prior["images"],
            "container_inspect": {
                name: {
                    "Id": item["container_id"], "Image": item["image_id"],
                    "Name": "/" + name, "Config": {}, "HostConfig": {}, "Mounts": [],
                    "State": {"Status": "exited"},
                }
                for name, item in containers.items()
            },
            "storage": prior["storage"],
            "authority": {"host_files": {}, "vm_files": {}, "installed": {},
                          "keeper": "missing"},
            "postgres": {"database_read": False, "binary_version": "16",
                         "data_pg_version": "17", "image_id": "postgres-image",
                         "rootfs_layers": ["sha256:" + "a" * 64]},
            "platform": {"boot_id": validated["request"]["boot_id"],
                         "unit": {"load_state": "not-found", "active_state": "inactive",
                                  "sub_state": "dead"}},
            "window_start_ns": 100, "window_end_ns": 100,
        }
        with (
            mock.patch.object(carry, "_validate_group2_reboot_postgres"),
            mock.patch.object(carry, "_validate_log_link"),
            mock.patch.object(carry, "_validate_reconcile_quiet_logs"),
        ):
            carry.validate_group2_post_finalize_reboot_stopped(validated, stopped)
        stopped_stage = {
            **stopped, "schema": "group2-post-finalize-reboot-stage-v1",
            "phase": "stopped", "window_start_ns": 100, "window_end_ns": 100,
        }
        keeper = {
            key: copy.deepcopy(value) for key, value in stopped_stage.items()
            if key != "platform"
        }
        keeper.update(
            phase="keeper_ready", window_start_ns=110, window_end_ns=120,
            authority={**stopped["authority"], "keeper": None},
            kernel={"boot_id": validated["request"]["boot_id"],
                    "namespace_evidence": {}, "unit": "dlr.service"},
        )
        keeper["authority"]["keeper"] = keeper["kernel"]
        with mock.patch.object(carry, "_validate_group2_reboot_empty_log_segment"):
            with (
                mock.patch.object(carry, "_validate_group2_reboot_keeper_kernel"),
                mock.patch.object(carry, "_validate_group2_reboot_parent_volume_identity"),
            ):
                carry.validate_group2_post_finalize_reboot_transition(
                    validated, stopped_stage, keeper, "keeper_ready"
                )
        database = {**copy.deepcopy(keeper), "phase": "database_ready",
                    "window_start_ns": 130, "window_end_ns": 140,
                    "db": {"rows": "same"},
                    "postgres": {"database_read": True, "schema": "0040_issue152_dispositions",
                                 "binary_version": "16", "server_version": "16",
                                 "data_pg_version": "17", "image_id": "postgres-image",
                                 "rootfs_layers": ["sha256:" + "a" * 64]},
                    "start_admission": {"gate": "raw", "configuration": {},
                                        "mutation_guard_rows": {}}}
        database.pop("kernel")
        for name in ("postgres", "rabbitmq"):
            database["containers"][name]["status"] = "running"
            database["containers"][name]["health"] = "healthy"
            database["container_inspect"][name]["State"]["Status"] = "running"
        with (
            mock.patch.object(carry, "validate_group2_reboot_start_admission"),
            mock.patch.object(carry, "_validate_group2_reboot_empty_log_segment"),
            mock.patch.object(carry, "_validate_group2_reboot_postgres"),
        ):
            carry.validate_group2_post_finalize_reboot_transition(
                validated, keeper, database, "database_ready"
            )
        application = {**copy.deepcopy(database), "phase": "applications_started",
                       "window_start_ns": 150, "window_end_ns": 200,
                       "kernel": copy.deepcopy(keeper["kernel"]),
                       "startup_request": {"raw": "worker"},
                       "startup_proof": {"nonce": "a" * 32},
                       "startup_files": {"changed": ["two-mtimes"]},
                       "rabbit_identity": {"raw": "broker", "captured_start_ns": 160,
                                           "captured_end_ns": 190},
                       "mutation_guard": {}}
        for name in ("control", "worker", "web"):
            application["containers"][name]["status"] = "running"
            application["containers"][name]["health"] = "healthy"
            application["container_inspect"][name]["State"]["Status"] = "running"
        application["startup_request"] = {
            "profile": validated["platform"]["account_entry"],
            "logs_before": database["logs"], "logs_after": application["logs"],
            "container_before": database["containers"]["worker"],
            "container_after": application["containers"]["worker"],
            "window_start_ns": 150, "window_end_ns": 200,
        }
        with (
            mock.patch.object(
                carry, "_same_container_worker_startup_proof",
                return_value=application["startup_proof"],
            ),
            mock.patch.object(
                carry, "compare_group2_startup_files",
                return_value=application["startup_files"],
            ),
            mock.patch.object(carry, "validate_group2_reboot_rabbit_identity"),
            mock.patch.object(carry, "_validate_group2_reboot_mutation_guard"),
            mock.patch.object(carry, "_validate_group2_reboot_postgres"),
            mock.patch.object(carry, "_validate_group2_reboot_running_kernel"),
            mock.patch.object(carry, "_validate_log_link"),
            mock.patch.object(carry, "_validate_reconcile_log_activity"),
        ):
            carry.validate_group2_post_finalize_reboot_transition(
                validated, database, application, "applications_started"
            )
            replaced = copy.deepcopy(application)
            replaced["containers"]["worker"]["container_id"] = "replacement"
            with self.assertRaises(carry.CarryForwardError):
                carry.validate_group2_post_finalize_reboot_transition(
                    validated, database, replaced, "applications_started"
                )
        reordered = copy.deepcopy(database)
        reordered["window_start_ns"] = 119
        with self.assertRaises(carry.CarryForwardError):
            carry.validate_group2_post_finalize_reboot_transition(
                validated, keeper, reordered, "database_ready"
            )

    def test_reboot_postgres_and_new_boot_keeper_evidence_are_sourceful(self):
        expected = {
            "schema": "0040_issue152_dispositions",
            "binary_version": "postgres (PostgreSQL) 16.15",
            "server_version": "16.15", "data_pg_version": "16",
        }
        stopped_pg = {
            "database_read": False, "binary_version": expected["binary_version"],
            "data_pg_version": "16", "image_id": "sha256:postgres",
            "rootfs_layers": ["sha256:" + "a" * 64],
        }
        self.assertEqual(
            carry._validate_group2_reboot_postgres(
                stopped_pg, expected, "sha256:postgres", running=False
            ),
            stopped_pg,
        )
        running_pg = {**stopped_pg, "database_read": True,
                      "schema": expected["schema"],
                      "server_version": expected["server_version"]}
        carry._validate_group2_reboot_postgres(
            running_pg, expected, "sha256:postgres", running=True,
            baseline=stopped_pg,
        )
        for mutation in (
            lambda value: value.__setitem__("schema", "0041_forged"),
            lambda value: value.__setitem__("rootfs_layers", []),
            lambda value: value.__setitem__("database_read", False),
        ):
            changed = copy.deepcopy(running_pg); mutation(changed)
            with self.assertRaises(carry.CarryForwardError):
                carry._validate_group2_reboot_postgres(
                    changed, expected, "sha256:postgres", running=True,
                    baseline=stopped_pg,
                )

        request = {
            "boot_id": "00000000-0000-4000-8000-000000000001",
            "prior": {
                "storage": [
                    {"service": "worker", "type": "volume", "source": "runtime",
                     "destination": "/var/lib/dlr/runtime", "read_only": False},
                    {"service": "worker", "type": "volume", "source": "journal",
                     "destination": "/var/lib/dlr/journal", "read_only": False},
                ],
                "parameters": {"keeper": {
                    "unit": "dlr-preview.service", "cpu_quota": "250%",
                    "memory_max": "2G",
                }},
            },
        }
        device = os.makedev(1, 2)
        host_mount = {"filesystem": "ext4", "major_minor": "1:2",
                      "root": "/", "mountpoint": "/"}
        volumes = {}
        for name in ("runtime", "journal"):
            mountpoint = f"/var/lib/docker/volumes/{name}/_data"
            mount = {**host_mount, "root": mountpoint}
            volumes[name] = {
                "name": name, "device": device, "inode": 10,
                "mount": mount,
                "inspect": {"Name": name, "Mountpoint": mountpoint},
                "mountpoint_stat": {"device": device, "inode": 10,
                                    "type": "directory", "mode": 0o755,
                                    "uid": 0, "gid": 0},
                "host_mount": host_mount,
                "derived_mount": {"filesystem": "ext4", "major_minor": "1:2",
                                  "root": mountpoint, "mountpoint": mountpoint},
            }
        backings = {
            "schema": "group2-post-finalize-reboot-volume-backings-v1",
            "boot_id": request["boot_id"], "host_mount_namespace": "mnt:[11]",
            "host_mountinfo": [
                {"filesystem": "proc", "major_minor": "0:7",
                 "root": "/", "mountpoint": "/proc"},
                host_mount,
            ],
            "volumes": volumes,
        }
        targets = sorted(
            (item["mount"]["filesystem"], item["mount"]["major_minor"],
             item["mount"]["root"])
            for item in volumes.values()
        )
        scan = {"task_count": 1, "namespace_count": 1, "mountinfo_bytes": 1,
                "fd_entries": 0, "related_count": 0, "pin_count": 0,
                "related": [], "pins": [],
                "target_digest": carry.digest({"related": [], "pins": []})}
        description = "DataLinkRuntime Sandbox dlr-preview.service CPU=250% Memory=2G"
        limit_keys = {
            "cpu.max": "250000 100000", "memory.max": str(2 * 1024 ** 3),
            "memory.swap.max": "0", "pids.max": "max",
            "cgroup.subtree_control": "cpu memory pids", "cgroup.procs": "",
            "uid": 0, "gid": 0,
        }
        kernel = {
            "boot_id": request["boot_id"], "unit": "dlr-preview.service",
            "control_group": "/system.slice/dlr-preview.service", "keeper_pid": 7,
            "keeper_starttime": "99", "description": description,
            "parent_device": 1, "parent_inode": 2,
            "children": {"agent": {"populated": 1, "process_count": 1,
                                    "process_digest": carry.digest([7]),
                                    "device": 1, "inode": 3}},
            "old_worker_authority": None,
            "namespace_evidence": {
                "schema": "group2-post-finalize-reboot-namespace-evidence-v1",
                "mode": "keeper_idle", "targets": targets,
                "scans": [copy.deepcopy(scan), copy.deepcopy(scan)],
            },
            "retired_markers": [],
            "unit_properties": {"ActiveState": "active", "Delegate": "yes",
                                "ControlGroup": "/system.slice/dlr-preview.service",
                                "MainPID": "7", "Description": description,
                                "InvocationID": "a" * 32},
            "cgroup_limits": {".": copy.deepcopy(limit_keys),
                              "agent": copy.deepcopy(limit_keys)},
            "volume_backings": backings,
        }
        self.assertEqual(carry._validate_group2_reboot_keeper_kernel(kernel, request), kernel)
        changed_inventory = copy.deepcopy(backings)
        changed_inventory["host_mountinfo"].append(
            {"filesystem": "sysfs", "major_minor": "0:8",
             "root": "/", "mountpoint": "/sys"}
        )
        self.assertTrue(carry._group2_reboot_backings_stable(
            backings, changed_inventory
        ))
        missing_inventory = copy.deepcopy(backings)
        missing_inventory["host_mountinfo"] = []
        with self.assertRaises(carry.CarryForwardError):
            carry._validate_group2_reboot_backing_derivations(
                missing_inventory, request, "group2_reboot_kernel_changed"
            )
        for mutation in (
            lambda value: value.__setitem__("keeper_pid", True),
            lambda value: value["namespace_evidence"]["scans"][0]["related"].append({}),
            lambda value: value["namespace_evidence"]["scans"][0].__setitem__(
                "task_count", 8193
            ),
            lambda value: value["cgroup_limits"]["."].__setitem__("cpu.max", "0 0"),
            lambda value: value["volume_backings"]["volumes"]["runtime"]
            ["derived_mount"].__setitem__("root", "/forged"),
        ):
            changed = copy.deepcopy(kernel); mutation(changed)
            with self.assertRaises(carry.CarryForwardError):
                carry._validate_group2_reboot_keeper_kernel(changed, request)

    def test_reboot_commands_are_joined_to_their_completed_stage(self):
        ids = {
            name: f"{name}-id"
            for name in ("postgres", "rabbitmq", "control", "worker", "web")
        }
        request = {
            "window": {"not_before_ns": 100, "deadline_ns": 300},
            "prior": {
                "parameters": {"keeper": {
                    "unit": "dlr-preview.service", "cpu_quota": "250%",
                    "memory_max": "2G",
                }},
                "containers": {name: {"container_id": value} for name, value in ids.items()},
            },
        }
        evidence = {
            "keeper_ready": {"window_start_ns": 110, "window_end_ns": 130},
            "database_ready": {"window_start_ns": 131, "window_end_ns": 160},
            "applications_started": {"window_start_ns": 161, "window_end_ns": 220},
        }
        times = {
            "prepare-keeper": (111, 120), "start-postgres": (132, 135),
            "start-rabbitmq": (136, 140), "start-control": (162, 165),
            "start-worker": (166, 170), "start-web": (171, 175),
        }
        commands = {}
        for action, (started, ended) in times.items():
            argv = (
                ["/tmp/prepare-sandbox-host.sh", "--unit", "dlr-preview.service",
                 "--cpu-quota", "250%", "--memory-max", "2G"]
                if action == "prepare-keeper"
                else ["docker", "start", ids[action.removeprefix("start-")]]
            )
            intent = {"schema": "group2-post-finalize-reboot-command-intent-v1",
                      "action": action, "argv": argv, "started_at_ns": started}
            result = {"schema": "group2-post-finalize-reboot-command-result-v1",
                      "action": action, "argv": argv, "started_at_ns": started,
                      "ended_at_ns": ended, "returncode": 0, "stdout": "", "stderr": ""}
            commands[action] = {
                "intent": {"sha256": carry.digest(intent),
                           "content_b64": base64.b64encode(carry.canonical_bytes(intent)).decode()},
                "result": {"sha256": carry.digest(result),
                           "content_b64": base64.b64encode(carry.canonical_bytes(result)).decode()},
            }
        carry._validate_group2_reboot_commands(commands, request, evidence)
        changed = copy.deepcopy(commands)
        intent = json.loads(base64.b64decode(changed["start-control"]["intent"]["content_b64"]))
        result = json.loads(base64.b64decode(changed["start-control"]["result"]["content_b64"]))
        intent["started_at_ns"] = result["started_at_ns"] = 150
        for key, value in (("intent", intent), ("result", result)):
            changed["start-control"][key] = {
                "sha256": carry.digest(value),
                "content_b64": base64.b64encode(carry.canonical_bytes(value)).decode(),
            }
        with self.assertRaises(carry.CarryForwardError):
            carry._validate_group2_reboot_commands(changed, request, evidence)

    def test_reboot_directory_is_single_fixed_boot_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            boot_id = "00000000-0000-4000-8000-000000000001"
            reboot = (
                root / "incidents" / carry.GROUP2_PARTIAL_INCIDENT_ID / "finalize"
                / carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID / "reboot"
            )
            directory = reboot / boot_id
            directory.mkdir(parents=True)
            self.assertEqual(
                carry._validate_group2_post_finalize_reboot_directory(
                    root, carry.GROUP2_PARTIAL_INCIDENT_ID,
                    carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID, boot_id,
                ),
                directory,
            )
            extra = reboot / "00000000-0000-4000-8000-000000000002"
            extra.mkdir()
            with self.assertRaisesRegex(
                carry.CarryForwardError, "group2_reboot_replay_rejected"
            ):
                carry._validate_group2_post_finalize_reboot_directory(
                    root, carry.GROUP2_PARTIAL_INCIDENT_ID,
                    carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID, boot_id,
                )
            extra.rmdir()
            directory.rmdir()
            target = reboot / "target"
            target.mkdir()
            directory.symlink_to(target, target_is_directory=True)
            target.rename(reboot / "00000000-0000-4000-8000-000000000002")
            with self.assertRaises(carry.CarryForwardError):
                carry._validate_group2_post_finalize_reboot_directory(
                    root, carry.GROUP2_PARTIAL_INCIDENT_ID,
                    carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID, boot_id,
                )

    def test_reboot_container_metadata_is_separate_and_mount_order_is_stable(self):
        normalized = {
            "container_id": "same-id", "image_id": "same-image",
            "status": "exited", "health": None, "started_at": "old",
            "restart_count": 0,
        }
        raw = {
            "Id": "same-id", "Image": "same-image", "Name": "/worker",
            "Config": {"Env": []}, "HostConfig": {"NetworkMode": "preview"},
            "Mounts": [
                {"Destination": "/z", "Source": "z"},
                {"Destination": "/a", "Source": "a"},
            ],
            "State": {"Status": "exited", "Pid": 0, "ExitCode": 0},
            "RestartCount": 0,
        }
        reordered = copy.deepcopy(raw)
        reordered["Mounts"].reverse()
        expected = {
            **normalized,
            "inspect_static_digest": carry.digest(carry._reboot_container_static(raw)),
            "stopped_state_digest": carry.digest(raw["State"]),
        }
        self.assertEqual(
            carry.digest(carry._reboot_container_static(raw)),
            carry.digest(carry._reboot_container_static(reordered)),
        )
        self.assertEqual(carry._reboot_expected_container(expected), normalized)
        carry._validate_reboot_container_raw(raw, expected, normalized, stopped=True)

    def test_reboot_artifact_inventory_uses_storage_keys_and_preserves_quarantine(self):
        key = "ab" + "1" * 62
        part = "cd" + "2" * 62
        quarantined = "ef" + "3" * 62
        entries = [
            {"path": name, "type": "directory"}
            for name in (
                "objects", "objects/ab", "parts", "parts/cd",
                "quarantine", "quarantine/ef",
            )
        ] + [
            {"path": f"objects/ab/{key}", "type": "file", "mtime_ns": 11},
            {"path": f"parts/cd/{part}.part", "type": "file", "mtime_ns": 12},
            {"path": f"quarantine/ef/{quarantined}", "type": "file", "mtime_ns": 13},
            {"path": f"quarantine/ef/{quarantined}.part", "type": "file", "mtime_ns": 14},
        ]
        inventory = carry._group2_reboot_artifact_inventory(entries)
        self.assertEqual([item["storage_key"] for item in inventory], [key, part])
        self.assertEqual([item["path"] for item in inventory], [
            f"objects/ab/{key}", f"parts/cd/{part}.part",
        ])
        for bad in (
            {"path": f"objects/ff/{key}", "type": "file", "mtime_ns": 1},
            {"path": f"objects/ab/{key}.part", "type": "file", "mtime_ns": 1},
            {"path": "objects/link", "type": "symlink"},
            {"path": "unknown", "type": "directory"},
        ):
            with self.assertRaises(carry.CarryForwardError):
                carry._group2_reboot_artifact_inventory([bad])

    def test_reboot_network_boundary_binds_all_original_endpoints(self):
        prior, raw, states, endpoints = {}, {}, {}, {}
        network_id = "network-id"
        for index, service in enumerate(
            ("postgres", "rabbitmq", "control", "worker", "web", "account-web"), 1
        ):
            container_id = f"{index:064x}"
            current = {
                "container_id": container_id, "image_id": f"image-{service}",
                "status": "running" if service in {"postgres", "rabbitmq"} else "exited",
                "health": "healthy" if service in {"postgres", "rabbitmq"} else None,
                "restart_count": 0,
            }
            running = service in {"postgres", "rabbitmq"}
            address = f"172.20.0.{index}" if running else ""
            inspect = {
                "Id": container_id, "Image": current["image_id"], "Name": f"/{service}",
                "Config": {}, "HostConfig": {"PortBindings": {}}, "Mounts": [],
                "State": {"Status": current["status"]}, "RestartCount": 0,
                "NetworkSettings": {"Networks": {"preview": {
                    "NetworkID": network_id,
                    "EndpointID": f"endpoint-{index}" if running else "",
                    "IPAddress": address,
                }}},
            }
            prior[service] = {
                **current,
                "inspect_static_digest": carry.digest(carry._reboot_container_static(inspect)),
                "stopped_state_digest": carry.digest(inspect["State"]),
            }
            raw[service], states[service] = inspect, current
            if running:
                endpoints[container_id] = {
                    "Name": service, "EndpointID": f"endpoint-{index}",
                    "MacAddress": "", "IPv4Address": address + "/16", "IPv6Address": "",
                }
        value = {
            "network_id": network_id, "container_inspect": raw,
            "network_inspect": {"Id": network_id, "Containers": endpoints},
            "container_states": states,
        }
        value["raw_digest"] = carry.digest(value)
        self.assertEqual(carry._validate_group2_reboot_network_boundary(value, prior), value)
        changed = copy.deepcopy(value)
        changed["network_inspect"]["Containers"]["foreign"] = {}
        changed["raw_digest"] = carry.digest({
            key: item for key, item in changed.items() if key != "raw_digest"
        })
        with self.assertRaises(carry.CarryForwardError):
            carry._validate_group2_reboot_network_boundary(changed, prior)

    def test_reboot_post_app_rabbit_identity_binds_slots_to_fresh_worker_ip(self):
        configuration = carry.derive_group2_reboot_configuration(
            {"Config": {"Env": ["DLR_RABBITMQ_URL=amqp://dlr:secret@rabbitmq:5672/%2F"]}},
            {"Config": {"Env": ["DLR_RABBITMQ_URL=amqp://dlr:secret@rabbitmq:5672/%2F",
                                   "DLR_WORKER_EXECUTION_SLOTS=2"]}},
            [{"id": 7, "name": "worker-1"}],
        )
        inspections = {}
        for index, service in enumerate(
            ("postgres", "rabbitmq", "control", "worker", "web", "account-web"), 1
        ):
            inspections[service] = {
                "Id": f"id-{service}", "Image": f"image-{service}",
                "Config": {"Env": copy.deepcopy(
                    configuration["control_inspect"]["Config"]["Env"] if service == "control"
                    else configuration["worker_inspect"]["Config"]["Env"] if service == "worker"
                    else []
                )},
                "State": {"Status": "running", "Pid": 100 + index,
                          "StartedAt": f"2026-09-21T00:00:0{index}Z"},
                "NetworkSettings": {"Networks": {"preview": {
                    "NetworkID": "net", "IPAddress": f"172.20.0.{index}",
                }}},
            }
        def mapped(address):
            octets = [int(item) for item in address.split(".")]
            return [0, 0, 0, 0, 0, 65535,
                    (octets[0] << 8) | octets[1], (octets[2] << 8) | octets[3]]

        rabbit_ip = "172.20.0.2"; control_ip = "172.20.0.3"; worker_ip = "172.20.0.4"
        connections = [
            {"pid": "conn-worker", "peer_host": mapped(worker_ip), "peer_port": 41000,
             "host": mapped(rabbit_ip), "port": 5672, "user": "dlr", "vhost": "/",
             "protocol": [0, 9, 1], "client_properties": [],
             "state": "running"},
            {"pid": "conn-control", "peer_host": mapped(control_ip), "peer_port": 41001,
             "host": mapped(rabbit_ip), "port": 5672, "user": "dlr", "vhost": "/",
             "protocol": [0, 9, 1], "client_properties": [],
             "state": "running"},
        ]
        channels = [
            {"pid": "chan-worker", "connection": "conn-worker", "user": "dlr",
             "vhost": "/", "number": 1, "consumer_count": 2,
             "messages_unacknowledged": 0, "prefetch_count": 0},
            {"pid": "chan-control", "connection": "conn-control", "user": "dlr",
             "vhost": "/", "number": 1, "consumer_count": 0,
             "messages_unacknowledged": 0, "prefetch_count": 0},
        ]
        consumers = [
            {"queue_name": "dlr.worker.7.q", "channel_pid": "chan-worker",
             "consumer_tag": f"dlr-worker-7-e9-s{slot}-t{slot + 1}",
             "ack_required": True, "prefetch_count": 1, "active": True,
             "arguments": []}
            for slot in range(2)
        ]
        binding = {}
        for service in ("rabbitmq", "control", "worker"):
            raw = inspections[service]; state = raw["State"]
            binding[service] = {
                "container_id": raw["Id"], "image_id": raw["Image"],
                "network_id": "net",
                "ip": next(iter(raw["NetworkSettings"]["Networks"].values()))["IPAddress"],
                "pid": state["Pid"], "started_at": state["StartedAt"],
                "config_env_digest": carry.digest(raw["Config"]["Env"]),
            }
        value = {
            "schema": "group2-post-finalize-reboot-rabbit-identity-v1",
            "captured_start_ns": 1, "captured_end_ns": 2,
            "container_binding": binding, "samples": [
                {"connections": connections, "channels": channels, "consumers": consumers},
                copy.deepcopy({"connections": connections, "channels": channels,
                               "consumers": consumers}),
            ],
        }
        value["raw_digest"] = carry.digest(value)
        self.assertEqual(
            carry.validate_group2_reboot_rabbit_identity(value, configuration, inspections),
            value,
        )
        for mutate in (
            lambda changed: changed["samples"][0]["consumers"].pop(),
            lambda changed: changed["samples"][0]["connections"][0].update(
                peer_host=mapped("172.20.0.99")),
            lambda changed: changed["samples"][0]["consumers"][0].update(queue_name="foreign"),
            lambda changed: changed["samples"][0]["consumers"][1].update(
                consumer_tag="dlr-worker-7-e9-s0-t2"),
        ):
            changed = copy.deepcopy(value); mutate(changed)
            changed["raw_digest"] = carry.digest({
                key: item for key, item in changed.items() if key != "raw_digest"
            })
            with self.assertRaises(carry.CarryForwardError):
                carry.validate_group2_reboot_rabbit_identity(
                    changed, configuration, inspections
                )

    def test_reboot_configuration_accepts_equal_proxy_case_aliases_only(self):
        control = {"Config": {"Env": [
            "DLR_RABBITMQ_URL=amqp://dlr:secret@rabbitmq:5672/%2F",
            "HTTP_PROXY=http://proxy.invalid", "http_proxy=http://proxy.invalid",
            "HTTPS_PROXY=http://proxy.invalid", "https_proxy=http://proxy.invalid",
            "NO_PROXY=localhost", "no_proxy=localhost",
        ]}}
        worker = {"Config": {"Env": [
            "DLR_RABBITMQ_URL=amqp://dlr:secret@rabbitmq:5672/%2F",
        ]}}
        derived = carry.derive_group2_reboot_configuration(
            control, worker, [{"id": 1, "name": "worker-1"}]
        )
        self.assertEqual(derived["worker_id"], 1)
        changed = copy.deepcopy(control)
        changed["Config"]["Env"][2] = "http_proxy=http://different.invalid"
        with self.assertRaises(carry.CarryForwardError):
            carry.derive_group2_reboot_configuration(
                changed, worker, [{"id": 1, "name": "worker-1"}]
            )

    def test_reboot_action_persists_intent_and_failure_before_raising(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            completed = subprocess.CompletedProcess(
                ["docker", "start", "postgres-id"], 17, "started\n", "failed\n"
            )
            with (
                mock.patch.object(carry.subprocess, "run", return_value=completed),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "group2_reboot_start_postgres_failed"
                ),
            ):
                carry._run_group2_reboot_action(
                    directory, "start-postgres",
                    ["docker", "start", "postgres-id"], 30,
                )
            intent = carry.read_private(directory / "command-start-postgres-intent.json")
            result = carry.read_private(directory / "command-start-postgres-result.json")
            self.assertEqual(intent["action"], "start-postgres")
            self.assertEqual(result["returncode"], 17)
            self.assertEqual(result["stdout"], "started\n")
            with self.assertRaisesRegex(
                carry.CarryForwardError, "group2_reboot_replay_rejected"
            ):
                carry._run_group2_reboot_action(
                    directory, "start-postgres",
                    ["docker", "start", "postgres-id"], 30,
                )

    def test_reboot_vm_failure_closes_phase_and_prevents_next_action_or_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            boot_id = "00000000-0000-4000-8000-000000000001"
            directory = (
                root / "incidents" / carry.GROUP2_PARTIAL_INCIDENT_ID / "finalize"
                / carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID / "reboot"
                / boot_id
            )
            directory.mkdir(parents=True, mode=0o700)
            request = {
                "incident_id": carry.GROUP2_PARTIAL_INCIDENT_ID,
                "finalize_id": carry.GROUP2_POST_FINALIZE_REBOOT_PARENT_FINALIZE_ID,
                "boot_id": boot_id, "request_digest": "a" * 64,
            }
            carry.write_private(directory / "request.json", request)
            carry.write_private(directory / "phase.json", {
                "schema": "group2-post-finalize-reboot-phase-v1",
                "incident_id": request["incident_id"],
                "finalize_id": request["finalize_id"], "boot_id": boot_id,
                "phase": "database_ready", "completed_stages": ["stopped"],
                "pending_action": "start-bound-control-worker-web",
                "evidence_digests": {"stopped": "b" * 64},
            })
            calls = []

            def fail_after_start(*_args):
                carry._run_group2_reboot_action(
                    directory, "start-control", ["docker", "start", "control-id"], 30,
                )
                calls.append("control")
                raise carry.CarryForwardError("group2_reboot_control_start_failed")

            completed = subprocess.CompletedProcess(
                ["docker", "start", "control-id"], 0, "control-id\n", ""
            )
            with (
                mock.patch.object(carry.subprocess, "run", return_value=completed),
                mock.patch.object(
                    carry, "_recover_group2_post_finalize_reboot_vm_once",
                    side_effect=fail_after_start,
                ) as execute,
                self.assertRaisesRegex(
                    carry.CarryForwardError, "group2_reboot_control_start_failed"
                ),
            ):
                carry.recover_group2_post_finalize_reboot_vm(
                    root, request["incident_id"], request["finalize_id"], boot_id
                )
            self.assertEqual(calls, ["control"])
            failure = carry.read_private(directory / "failure.json")
            self.assertEqual(failure["code"], "group2_reboot_control_start_failed")
            self.assertIn("start-control", failure["action_records"])
            self.assertEqual(
                carry.read_private(directory / "phase.json")["pending_action"],
                "failure.json",
            )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "group2_reboot_replay_rejected"
            ):
                carry.recover_group2_post_finalize_reboot_vm(
                    root, request["incident_id"], request["finalize_id"], boot_id
                )
            self.assertEqual(execute.call_count, 1)
            self.assertFalse((directory / "command-start-worker-intent.json").exists())

    def _reboot_admission(self):
        topology = {
            "exchanges": [
                {"name": "dlr.execution.dispatch.v1", "type": "direct", "durable": True},
                {"name": "dlr.execution.infrastructure.dlx", "type": "direct", "durable": True},
            ],
            "bindings": [
                {"source_name": "dlr.execution.dispatch.v1",
                 "destination_name": "dlr.worker.1.q", "destination_kind": "queue",
                 "routing_key": "worker.1", "arguments": []},
                {"source_name": "dlr.execution.infrastructure.dlx",
                 "destination_name": "dlr.execution.infrastructure.dlq",
                 "destination_kind": "queue", "routing_key": "infrastructure",
                 "arguments": []},
                {"source_name": "", "destination_name": "dlr.worker.1.q",
                 "destination_kind": "queue", "routing_key": "dlr.worker.1.q",
                 "arguments": []},
                {"source_name": "",
                 "destination_name": "dlr.execution.infrastructure.dlq",
                 "destination_kind": "queue",
                 "routing_key": "dlr.execution.infrastructure.dlq",
                 "arguments": []},
            ],
            "policies": [],
            "operator_policies": [],
        }
        parameters = {"frozen": "parent"}
        raw = {name: [] for name in carry.GROUP2_REBOOT_ADMISSION_TABLES}
        raw["users"] = [{"id": 1, "username": "admin"}]
        raw["credentials"] = [
            {"id": 1, "name": "demo-passwd"}, {"id": 2, "name": "demo-token"},
        ]
        raw["workers"] = [{"id": 1, "name": "worker-1"}]
        raw["global_execution_admission"] = [
            {"singleton_key": "global", "outstanding_count": 0, "outstanding_bytes": 0}
        ]
        raw["managed_input_capacity"] = [
            {"id": 1, "actual_bytes": 0, "reserved_bytes": 0}
        ]
        raw["runtime_reconciliation_cursors"] = [
            {"name": "expired_attempts", "after_id": 0, "upper_id": 0}
        ]
        projection = {}
        database_rows = {}
        for name, columns in carry.GROUP2_REBOOT_ADMISSION_COLUMNS.items():
            rows = raw[name]
            primary_key = list(carry.GROUP2_REBOOT_ADMISSION_PRIMARY_KEYS[name])
            ordered = sorted(
                rows,
                key=lambda row: carry.canonical_bytes(
                    [row[key] for key in primary_key]
                ),
            )
            projection[name] = {
                "columns": sorted(columns), "primary_key": primary_key,
                "rows": [carry.row_digest(row) for row in ordered],
                "count": len(rows),
            }
            database_rows[name] = {
                "columns": sorted(columns), "primary_key": primary_key,
                "rows": copy.deepcopy(rows),
            }
        database = {"projection": projection, "asset_projection": {},
                    "protected_rows": {"tables": {}}}
        configuration = carry.derive_group2_reboot_configuration(
            {"Config": {"Env": ["DLR_MASTER_KEY=configured",
                                   "DLR_RABBITMQ_URL=amqp://dlr:secret@rabbitmq:5672/%2F"]}},
            {"Config": {"Env": ["DLR_RABBITMQ_URL=amqp://dlr:secret@rabbitmq:5672/%2F"]}},
            raw["workers"],
        )
        settings = configuration["effective_settings"]
        raw_files = {
            "journal_facts": {"attempt": [], "cleanup": [], "sandbox_recovery": []},
            "artifact_store": [],
        }
        derived = carry.derive_group2_reboot_start_admission(
            raw, raw_files, settings, t0_ns=10_000_000_000,
            horizon_ns=30_000_000_000,
            preserved_queued_ids=[], worker_id=1,
        )
        rabbit = {
            "image_id": "sha256:" + "a" * 64, "version": "4.3.5",
            "plugins": ["rabbitmq_management"],
            "config_digest": carry.digest(configuration["rabbitmq"]),
            "queue_scope": copy.deepcopy(configuration["rabbitmq"]["queues"]),
            "topology": topology, "raw_response_digest": "",
        }
        queues = [
            {
                **item,
                "arguments": [
                    [key, "signedint" if isinstance(value, int) else "longstr", value]
                    for key, value in item["arguments"].items()
                ],
                "messages_total": 0, "messages_ready": 0,
                "messages_unacknowledged": 0,
                "effective_policy_definition": {},
            }
            for item in configuration["rabbitmq"]["queues"]
        ]
        rabbit["queues"] = [
            {**item, "ra": {
                "total": {"raw": "{ok,0,{ra,node}}", "value": 0},
                "dlx": {"raw": "{ok,{0,0},{ra,node}}", "value": [0, 0]},
                "checked_out": {"raw": "{ok,0,{ra,node}}", "value": 0},
            }} for item in queues
        ]
        rabbit["external_before"] = {"connections": [], "channels": [], "consumers": []}
        rabbit["external_after"] = copy.deepcopy(rabbit["external_before"])
        rabbit["network"] = {"synthetic": "component-only"}
        rabbit["feature_flags"] = [
            {"name": name, "state": "enabled"}
            for name in ("feature_flags_v2", "quorum_queue", "stream_queue", "rabbitmq_4.3.0")
        ]
        rabbit["raw_response_digest"] = carry.digest(
            {"queues": rabbit["queues"], "topology": topology,
             "feature_flags": rabbit["feature_flags"],
             "external_before": rabbit["external_before"],
             "external_after": rabbit["external_after"],
             "network": rabbit["network"]}
        )
        value = {
            "schema": "group2-post-finalize-reboot-start-admission-v1",
            "database_digest": carry.digest(database),
            "parameters_digest": carry.digest(parameters),
            "clock": {"db_utc_ns": 10_000_000_000, "vm_lower_ns": 9_000_000_000,
                      "vm_upper_ns": 11_000_000_000},
            "deadline_ns": 20_000_000_000,
            "successor_deadline_ns": 30_000_000_000,
            "sources": copy.deepcopy(carry.GROUP2_POST_FINALIZE_REBOOT_SOURCES),
            "raw_tables": raw, "database_rows": database_rows,
            "mutation_guard_rows": {
                name: {
                    "columns": sorted(
                        {key for row in raw[name] for key in row}
                        or set(carry.GROUP2_REBOOT_ADMISSION_PRIMARY_KEYS[name])
                    ),
                    "primary_key": list(carry.GROUP2_REBOOT_ADMISSION_PRIMARY_KEYS[name]),
                    "rows": copy.deepcopy(raw[name]),
                }
                for name in carry.GROUP2_REBOOT_MUTATION_GUARD_TABLES
            },
            "raw_files": raw_files, "configuration": configuration,
            "effective_settings": settings, "preserved_queued_ids": [],
            "worker_id": 1, "rabbitmq": rabbit, "derived": derived,
        }
        value["mutation_guard_rows"]["managed_input_capacity"] = {
            "columns": ["id", "actual_bytes", "reserved_bytes", "updated_at"],
            "primary_key": ["id"],
            "rows": [{"id": 1, "actual_bytes": 0, "reserved_bytes": 0,
                      "updated_at": "2026-09-21T00:00:00+00:00"}],
        }
        return value, database, parameters

    def test_reboot_admission_derives_candidates_and_complete_rabbit_totals(self):
        self.assertEqual(
            carry._group2_admission_ns(
                "1970-01-02T00:00:00.000001+00:00", "test_invalid"
            ),
            86_400_000_001_000,
        )
        value, database, parameters = self._reboot_admission()
        self.assertEqual(carry.validate_group2_reboot_start_admission(
            value, database, parameters, 20_000_000_000, 30_000_000_000,
            "sha256:" + "a" * 64), value)
        operator_policy = copy.deepcopy(value["rabbitmq"]["topology"])
        operator_policy["operator_policies"] = [{
            "vhost": "/", "name": "expire-sink", "pattern": "^dlr\\.",
            "apply-to": "queues", "definition": {"message-ttl": 60000},
            "priority": 0,
        }]
        with self.assertRaises(carry.CarryForwardError):
            carry._validate_group2_rabbit_topology(
                operator_policy, value["configuration"], value["rabbitmq"]["plugins"]
            )
        with self.assertRaises(carry.CarryForwardError):
            carry._group2_rabbit_amqp_table([
                ["x-queue-type", "longstr", "quorum"],
                ["x-queue-type", "longstr", "classic"],
            ])
        mutations = (
            lambda changed: changed["sources"]["retention"].update({"caller.py": "0" * 64}),
            lambda changed: changed["raw_tables"]["execution_outbox"].append(
                {"id": 7, "execution_id": 8, "dispatch_generation": 1,
                 "status": "pending", "available_at": 151, "lease_expires_at": None}),
            lambda changed: changed["raw_files"]["journal_facts"]["attempt"].append({"id": 1}),
            lambda changed: changed["rabbitmq"]["queues"][0]["ra"]["dlx"].update(
                {"raw": "{ok,{1,13},{ra,node}}", "value": [1, 13]}),
            lambda changed: changed["rabbitmq"]["queues"].pop(),
        )
        for mutate in mutations:
            changed = copy.deepcopy(value); mutate(changed)
            with self.assertRaises(carry.CarryForwardError):
                carry.validate_group2_reboot_start_admission(
                    changed, database, parameters, 20_000_000_000,
                    30_000_000_000, "sha256:" + "a" * 64)
        changed = copy.deepcopy(value)
        changed["rabbitmq"]["feature_flags"][0]["state"] = "disabled"
        changed["rabbitmq"]["raw_response_digest"] = carry.digest({
            key: changed["rabbitmq"][key]
            for key in ("queues", "topology", "feature_flags", "external_before",
                        "external_after", "network")
        })
        with self.assertRaises(carry.CarryForwardError):
            carry.validate_group2_reboot_start_admission(
                changed, database, parameters, 20_000_000_000,
                30_000_000_000, "sha256:" + "a" * 64)

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
            "roots": [
                {
                    "path": "/logs/control",
                    "device": 1,
                    "inode": 9,
                    "mode": 0o750,
                    "uid": 3,
                    "gid": 4,
                    "mtime_ns": 5,
                    "entries": [
                        {
                            "path": "control.log",
                            "type": "file",
                            "device": common["device"],
                            "inode": common["inode"],
                            "mode": common["mode"],
                            "uid": common["uid"],
                            "gid": common["gid"],
                        }
                    ],
                    "allowed_new_files": ["control.log"],
                }
            ],
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
            "roots": copy.deepcopy(before["roots"]),
            "clock": precise_log_clock(20),
            "baseline_evidence_digest": before["evidence_digest"],
            "observed_after_ns": 20,
        }
        after["evidence_digest"] = carry.digest(after)
        return before, after

    def test_reboot_empty_log_segment_uses_real_append_contract(self):
        before, after = self._log_window("")
        carry._validate_group2_reboot_empty_log_segment(before, after)
        changed = copy.deepcopy(after)
        self._replace_log_text(changed, "new line\n")
        with self.assertRaises(carry.CarryForwardError):
            carry._validate_group2_reboot_empty_log_segment(before, changed)

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

    def test_log_endpoint_tree_is_canonical_complete_and_unambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "worker" / "worker.log"
            worker.parent.mkdir()
            worker.write_text("old\n")
            history = worker.parent / "history"
            history.mkdir()
            (history / "older.log").write_text("older\n")
            (history / "empty").mkdir()
            profile = self._profile(worker)
            endpoint = carry.capture_log_prefix(profile)
            carry._validate_log_endpoint(endpoint, profile)
            missing_parent = copy.deepcopy(endpoint)
            missing_parent["files"].append(
                {
                    "path": str(worker.parent / "not-yet" / "child.log"),
                    "exists": False,
                }
            )
            carry._validate_log_endpoint(missing_parent, profile)

            worker_root = next(
                item
                for item in endpoint["roots"]
                if item["path"] == str(worker.parent)
            )

            def rejected(edit):
                changed = copy.deepcopy(endpoint)
                changed_root = next(
                    item
                    for item in changed["roots"]
                    if item["path"] == str(worker.parent)
                )
                edit(changed, changed_root)
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "log_evidence_(invalid|link_invalid)"
                ):
                    carry._validate_log_endpoint(changed, profile)

            rejected(
                lambda _value, log_root: log_root.__setitem__(
                    "entries",
                    [item for item in log_root["entries"] if item["path"] != "history"],
                )
            )

            def file_parent(_value, log_root):
                parent = next(
                    item for item in log_root["entries"] if item["path"] == "history"
                )
                parent["type"] = "file"
                parent.pop("mtime_ns")

            rejected(file_parent)
            rejected(
                lambda value, _log_root: value["files"].append(
                    {"path": str(worker.parent), "exists": False}
                )
            )
            rejected(
                lambda value, _log_root: value["files"].append(
                    {"path": str(worker.parent) + "/./worker.log", "exists": False}
                )
            )
            rejected(
                lambda value, _log_root: value["files"].append(
                    {"path": str(worker.parent / "history"), "exists": False}
                )
            )
            rejected(
                lambda value, _log_root: value["files"].append(
                    {"path": str(worker / "child.log"), "exists": False}
                )
            )
            rejected(
                lambda value, _log_root: value["files"].append(
                    {"path": str(worker.parent / "history" / ".." / "missing.log"), "exists": False}
                )
            )
            rejected(
                lambda value, _log_root: value["files"].append(
                    {"path": str(worker.parent) + "/bad\x00.log", "exists": False}
                )
            )
            rejected(
                lambda _value, log_root: log_root["entries"][0].__setitem__(
                    "path", "bad\x00entry"
                )
            )
            def nul_allowed(_value, log_root):
                log_root["allowed_new_files"].append("bad\x00.log")
                log_root["allowed_new_files"].sort()

            rejected(nul_allowed)

            def overlapping_root(value, _log_root):
                nested = copy.deepcopy(worker_root)
                nested["path"] = str(history)
                nested["inode"] += 1000
                nested["entries"] = []
                nested["allowed_new_files"] = []
                value["roots"].append(nested)

            rejected(overlapping_root)

            account_log = root / "account-web" / "access.log"
            account_log.mkdir()
            with self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_link_invalid"
            ):
                carry.capture_log_prefix(profile)

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
            carry.combine_group2_log_window(baseline, first)
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

    def test_log_endpoint_file_and_root_views_must_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "worker" / "worker.log"
            worker.parent.mkdir()
            worker.write_text("before\n")
            profile = self._profile(worker)
            baseline = carry.capture_log_prefix(profile)
            after = carry.read_log_append(baseline)
            carry._validate_log_link(baseline, after, profile["profile_digest"])

            phantom = copy.deepcopy(after)
            account_root = next(
                item
                for item in phantom["roots"]
                if item["path"] == str(root / "account-web")
            )
            account_root["entries"].append(
                {
                    "path": "access.log",
                    "type": "file",
                    "device": account_root["device"],
                    "inode": 987654321,
                    "mode": 0o600,
                    "uid": account_root["uid"],
                    "gid": account_root["gid"],
                }
            )
            account_root["entries"].sort(key=lambda item: item["path"])
            account_root["mtime_ns"] = max(
                account_root["mtime_ns"], baseline["clock"]["lower_bound_ns"]
            )
            self._reseal_log(phantom)
            for operation in (
                lambda: carry._validate_log_link(
                    baseline, phantom, profile["profile_digest"]
                ),
                lambda: carry.combine_group2_log_window(baseline, phantom),
            ):
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "log_evidence_link_invalid"
                ):
                    operation()

            for key, value in (("inode", -1), ("mode", True)):
                invalid = copy.deepcopy(after)
                invalid_root = next(
                    item
                    for item in invalid["roots"]
                    if item["path"] == str(root / "account-web")
                )
                invalid_file = next(
                    item
                    for item in invalid["files"]
                    if item["path"] == str(root / "account-web" / "access.log")
                )
                invalid_entry = {
                    "path": "access.log",
                    "type": "file",
                    "device": invalid_root["device"],
                    "inode": 987654321,
                    "mode": 0o600,
                    "uid": invalid_root["uid"],
                    "gid": invalid_root["gid"],
                }
                invalid_entry[key] = value
                invalid_file.update(
                    {
                        "exists": True,
                        "device": invalid_entry["device"],
                        "inode": invalid_entry["inode"],
                        "mode": invalid_entry["mode"],
                        "uid": invalid_entry["uid"],
                        "gid": invalid_entry["gid"],
                        "size": 0,
                        "prefix_sha256": hashlib.sha256(b"").hexdigest(),
                        "appended_text": "",
                        "end_size": 0,
                        "end_sha256": hashlib.sha256(b"").hexdigest(),
                    }
                )
                invalid_root["entries"].append(invalid_entry)
                invalid_root["entries"].sort(key=lambda item: item["path"])
                invalid_root["mtime_ns"] = max(
                    invalid_root["mtime_ns"],
                    baseline["clock"]["lower_bound_ns"],
                )
                self._reseal_log(invalid)
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "log_evidence_invalid"
                ):
                    carry._validate_log_link(
                        baseline, invalid, profile["profile_digest"]
                    )

            for mutation in ("missing", "identity"):
                changed_before = copy.deepcopy(baseline)
                changed_after = copy.deepcopy(after)
                for endpoint in (changed_before, changed_after):
                    worker_root = next(
                        item
                        for item in endpoint["roots"]
                        if item["path"] == str(worker.parent)
                    )
                    worker_entry = next(
                        item
                        for item in worker_root["entries"]
                        if item["path"] == worker.name
                    )
                    if mutation == "missing":
                        worker_root["entries"].remove(worker_entry)
                    else:
                        worker_entry["inode"] += 1
                self._reseal_log(changed_before)
                changed_after["baseline_evidence_digest"] = changed_before[
                    "evidence_digest"
                ]
                self._reseal_log(changed_after)
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "log_evidence_link_invalid"
                ):
                    carry._validate_log_link(
                        changed_before,
                        changed_after,
                        profile["profile_digest"],
                    )

            empty_before = copy.deepcopy(baseline)
            empty_after = copy.deepcopy(after)
            empty_before["roots"] = []
            empty_after["roots"] = []
            self._reseal_log(empty_before)
            empty_after["baseline_evidence_digest"] = empty_before[
                "evidence_digest"
            ]
            self._reseal_log(empty_after)
            with self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_invalid"
            ):
                carry._validate_log_link(
                    empty_before, empty_after, profile["profile_digest"]
                )

            forged_baseline = copy.deepcopy(baseline)
            forged_worker = next(
                item
                for item in forged_baseline["files"]
                if item["path"] == str(worker)
            )
            forged_worker.clear()
            forged_worker.update(path=str(worker), exists=False)
            self._reseal_log(forged_baseline)
            worker.write_text("replacement-data\n")
            with self.assertRaisesRegex(
                carry.CarryForwardError, "log_evidence_link_invalid"
            ):
                carry.read_log_append(forged_baseline)

    def test_log_snapshot_has_a_fixed_prefix_under_scheduled_file_races(self):
        original = b"x" * (1024 * 1024 + 17) + b"\n"
        tail = b"legal-append\n"

        def exercise(event, stage, *, append_before=False):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "worker" / "worker.log"
                path.parent.mkdir()
                path.write_bytes(original)
                profile = self._profile(path)
                baseline = carry.capture_log_prefix(profile)
                baseline_item = next(
                    item for item in baseline["files"] if item["path"] == str(path)
                )
                if append_before:
                    with path.open("ab") as stream:
                        stream.write(tail)
                replacement = Path(directory) / "replacement.tmp"
                replacement.write_bytes(path.read_bytes())
                original_open = carry.os.open
                original_read = carry.os.read
                original_fstat = carry.os.fstat
                read_calls = 0
                read_lengths = []
                fstat_calls = 0
                injected = False

                def inject():
                    nonlocal injected
                    injected = True
                    info = path.stat()
                    if event == "append":
                        with path.open("ab") as stream:
                            stream.write(tail)
                    elif event == "replace":
                        os.replace(replacement, path)
                    elif event == "symlink":
                        path.unlink()
                        path.symlink_to(replacement)
                    elif event == "overwrite":
                        with path.open("r+b", buffering=0) as stream:
                            stream.write(b"X" * len(original))
                    elif event == "new-window-overwrite":
                        with path.open("r+b", buffering=0) as stream:
                            stream.seek(len(original))
                            stream.write(b"Y" * len(tail))
                        os.utime(
                            path,
                            ns=(info.st_atime_ns, info.st_mtime_ns + 1),
                        )
                        self.assertNotEqual(path.stat().st_mtime_ns, info.st_mtime_ns)
                    elif event == "overwrite-hide-mtime-change-atime":
                        with path.open("r+b", buffering=0) as stream:
                            stream.write(b"W" * len(original))
                        os.utime(
                            path,
                            ns=(info.st_atime_ns + 2_000_000_000, info.st_mtime_ns),
                        )
                    elif event == "truncate-regrow":
                        path.write_bytes(b"Z" * info.st_size)
                    elif event == "chmod":
                        path.chmod(0o600)
                    elif event != "none":
                        raise AssertionError(event)

                def opened(value, flags, *args, **kwargs):
                    if (
                        not injected
                        and stage == "before-open"
                        and str(value) == str(path)
                    ):
                        inject()
                    return original_open(value, flags, *args, **kwargs)

                def read(descriptor, length):
                    nonlocal read_calls
                    value = original_read(descriptor, length)
                    if original_fstat(descriptor).st_ino == baseline_item["inode"]:
                        read_calls += 1
                        read_lengths.append(length)
                        if not injected and stage == f"read-{read_calls}":
                            inject()
                    return value

                def fstat(descriptor):
                    nonlocal fstat_calls
                    value = original_fstat(descriptor)
                    if value.st_ino == baseline_item["inode"]:
                        fstat_calls += 1
                        if (
                            not injected
                            and stage == "after-final-fstat"
                            and fstat_calls == 2
                        ):
                            inject()
                    return value

                with mock.patch.object(
                    carry.os, "open", side_effect=opened
                ), mock.patch.object(
                    carry.os, "read", side_effect=read
                ), mock.patch.object(carry.os, "fstat", side_effect=fstat):
                    evidence = carry.read_log_append(baseline)
                return evidence, path.read_bytes(), injected, read_lengths

        positives = (
            ("none", "none", False),
            ("append", "before-open", False),
            ("append", "read-1", False),
            ("append", "read-2", False),
        )
        for event, stage, append_before in positives:
            with self.subTest(event=event, stage=stage):
                evidence, actual, injected, read_lengths = exercise(
                    event, stage, append_before=append_before
                )
                item = next(
                    value
                    for value in evidence["files"]
                    if value["path"].endswith("/worker/worker.log")
                )
                expected_size = len(original) + (
                    len(tail) if stage == "before-open" else 0
                )
                expected_text = tail.decode() if stage == "before-open" else ""
                self.assertEqual(injected, event != "none")
                self.assertEqual(item["end_size"], expected_size)
                self.assertEqual(item["appended_text"], expected_text)
                self.assertEqual(sum(read_lengths), 2 * expected_size)
                self.assertEqual(
                    hashlib.sha256(actual[: item["end_size"]]).hexdigest(),
                    item["end_sha256"],
                )

        negatives = (
            ("replace", "read-1", False, "log_file_unstable"),
            ("replace", "read-2", False, "log_file_unstable"),
            ("replace", "after-final-fstat", False, "log_file_unstable"),
            ("symlink", "read-1", False, "log_file_unstable"),
            ("overwrite", "read-1", False, "log_file_unstable"),
            ("truncate-regrow", "read-1", False, "log_file_unstable"),
            ("chmod", "read-1", False, "log_file_unstable"),
            ("overwrite", "before-open", False, "log_file_unstable"),
            (
                "overwrite-hide-mtime-change-atime",
                "before-open",
                False,
                "log_file_unstable",
            ),
            ("new-window-overwrite", "read-1", True, "log_file_unstable"),
        )
        for event, stage, append_before, code in negatives:
            with self.subTest(event=event, stage=stage), self.assertRaisesRegex(
                carry.CarryForwardError, code
            ):
                exercise(event, stage, append_before=append_before)

    def test_log_snapshot_fifo_replacement_returns_without_blocking(self):
        source = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import carry_forward as carry
path = Path(sys.argv[2])
path.write_bytes(b"preserved baseline\\n")
expected = path.lstat()
original_open = carry.os.open
def opened(value, flags, *args, **kwargs):
    path.unlink()
    os.mkfifo(path, 0o600)
    return original_open(value, flags, *args, **kwargs)
carry.os.open = opened
try:
    carry._read_stable_log(path, expected)
except carry.CarryForwardError as error:
    if str(error) == "log_file_unstable":
        print("REJECTED")
        raise SystemExit(0)
    raise
raise AssertionError("FIFO was accepted")
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.log"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    source,
                    str(Path(carry.__file__).parent),
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "REJECTED\n")

    def test_log_snapshot_resolves_only_bounded_append_metadata_lag(self):
        def exercise(*, append_after_lag, shrink_during_settle=False):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "worker" / "worker.log"
                path.parent.mkdir()
                original = b"preserved prefix\n"
                tail = b"later append\n"
                path.write_bytes(original)
                profile = self._profile(path)
                baseline = carry.capture_log_prefix(profile)
                item = next(
                    value for value in baseline["files"] if value["path"] == str(path)
                )
                original_fstat = carry.os.fstat
                fstat_calls = 0
                lag_injected = False
                append_injected = False
                shrink_injected = False

                def fstat(descriptor):
                    nonlocal fstat_calls, lag_injected, shrink_injected
                    value = original_fstat(descriptor)
                    if value.st_ino == item["inode"]:
                        fstat_calls += 1
                        if fstat_calls == 2:
                            lag_injected = True
                            os.utime(
                                path,
                                ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000),
                            )
                        elif fstat_calls == 3 and shrink_during_settle:
                            self.assertGreater(value.st_size, len(original))
                            path.write_bytes(path.read_bytes()[: len(original) + 5])
                            shrink_injected = True
                    return value

                def pause(_seconds):
                    nonlocal append_injected
                    if append_after_lag and not append_injected:
                        append_injected = True
                        with path.open("ab", buffering=0) as stream:
                            stream.write(tail)

                with mock.patch.object(
                    carry.os, "fstat", side_effect=fstat
                ), mock.patch.object(carry.time, "sleep", side_effect=pause) as sleep:
                    if not append_after_lag or shrink_during_settle:
                        with self.assertRaisesRegex(
                            carry.CarryForwardError, "log_file_unstable"
                        ):
                            carry.read_log_append(baseline)
                        self.assertTrue(lag_injected)
                        if shrink_during_settle:
                            self.assertEqual(sleep.call_count, 1)
                            self.assertTrue(append_injected)
                            self.assertTrue(shrink_injected)
                        else:
                            self.assertEqual(sleep.call_count, 3)
                            self.assertFalse(append_injected)
                        return
                    evidence = carry.read_log_append(baseline)

                endpoint = next(
                    value
                    for value in evidence["files"]
                    if value["path"] == str(path)
                )
                self.assertTrue(lag_injected)
                self.assertTrue(append_injected)
                self.assertEqual(sleep.call_count, 1)
                self.assertEqual(endpoint["end_size"], len(original))
                self.assertEqual(endpoint["appended_text"], "")
                self.assertEqual(
                    endpoint["end_sha256"], hashlib.sha256(original).hexdigest()
                )

        exercise(append_after_lag=True)
        exercise(append_after_lag=False)
        exercise(append_after_lag=True, shrink_during_settle=True)

    def test_log_snapshot_rejects_path_shrink_that_remains_above_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker" / "worker.log"
            path.parent.mkdir()
            original = b"preserved prefix\n"
            window = b"sampled append\n"
            later = b"later append\n"
            path.write_bytes(original)
            profile = self._profile(path)
            baseline = carry.capture_log_prefix(profile)
            with path.open("ab", buffering=0) as stream:
                stream.write(window)
            sampled_size = path.stat().st_size
            original_read = carry.os.read
            original_fstat = carry.os.fstat
            read_injected = False
            fstat_calls = 0
            truncate_injected = False

            def read(descriptor, length):
                nonlocal read_injected
                value = original_read(descriptor, length)
                if not read_injected:
                    read_injected = True
                    with path.open("ab", buffering=0) as stream:
                        stream.write(later)
                return value

            def fstat(descriptor):
                nonlocal fstat_calls, truncate_injected
                value = original_fstat(descriptor)
                fstat_calls += 1
                if fstat_calls == 2:
                    self.assertGreater(value.st_size, sampled_size)
                    path.write_bytes(path.read_bytes()[:sampled_size])
                    truncate_injected = True
                return value

            with mock.patch.object(
                carry.os, "read", side_effect=read
            ), mock.patch.object(
                carry.os, "fstat", side_effect=fstat
            ), self.assertRaisesRegex(carry.CarryForwardError, "log_file_unstable"):
                carry.read_log_append(baseline)
            self.assertTrue(read_injected)
            self.assertTrue(truncate_injected)
            self.assertEqual(path.stat().st_size, sampled_size)

    def test_log_snapshot_accepts_ten_reads_during_real_24mb_append_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker" / "worker.log"
            path.parent.mkdir()
            block = b"x" * 1023 + b"\n"
            path.write_bytes(block * (23 * 1024))
            profile = self._profile(path)
            baseline = carry.capture_log_prefix(profile)
            baseline_item = next(
                item for item in baseline["files"] if item["path"] == str(path)
            )
            stop = threading.Event()
            begun = threading.Event()

            def writer():
                with path.open("ab", buffering=0) as stream:
                    while not stop.is_set():
                        stream.write(b"legal append from owned thread\n")
                        begun.set()
                        stop.wait(0.0005)

            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(begun.wait(2))
            segments = []
            try:
                for _ in range(10):
                    segment = carry.read_log_append(baseline)
                    carry._validate_log_link(
                        baseline, segment, profile["profile_digest"]
                    )
                    segments.append(segment)
            finally:
                stop.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(segments), 10)
            content = path.read_bytes()
            self.assertEqual(
                hashlib.sha256(content[: baseline_item["size"]]).hexdigest(),
                baseline_item["prefix_sha256"],
            )
            carry.combine_group2_log_window(baseline, segments[-1])

    def test_log_snapshot_retries_only_a_partial_utf8_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker" / "worker.log"
            path.parent.mkdir()
            path.write_bytes(b"before\n")
            profile = self._profile(path)
            baseline = carry.capture_log_prefix(profile)
            with path.open("ab") as stream:
                stream.write(b"\xe2")
            completed = False

            def complete_tail(_seconds):
                nonlocal completed
                if not completed:
                    completed = True
                    with path.open("ab") as stream:
                        stream.write(b"\x82\xac\n")

            with mock.patch.object(
                carry, "_read_stable_log", wraps=carry._read_stable_log
            ) as sample, mock.patch.object(
                carry.time, "sleep", side_effect=complete_tail
            ) as pause:
                evidence = carry.read_log_append(baseline)
            self.assertEqual(sample.call_count, 2)
            self.assertEqual(pause.call_count, 1)
            worker = next(
                item for item in evidence["files"] if item["path"] == str(path)
            )
            self.assertEqual(worker["appended_text"], "€\n")

            second = carry.read_log_append(evidence)
            with path.open("ab") as stream:
                stream.write(b"\xe2")
            with mock.patch.object(
                carry, "_read_stable_log", wraps=carry._read_stable_log
            ) as sample, mock.patch.object(
                carry.time, "sleep", return_value=None
            ) as pause, self.assertRaisesRegex(
                carry.CarryForwardError, "log_append_partial_utf8"
            ):
                carry.read_log_append(second)
            self.assertEqual(sample.call_count, 3)
            self.assertEqual(pause.call_count, 2)
            with path.open("ab") as stream:
                stream.write(b"\xff")
            with mock.patch.object(
                carry, "_read_stable_log", wraps=carry._read_stable_log
            ) as sample, mock.patch.object(
                carry.time, "sleep", side_effect=AssertionError("must not retry")
            ), self.assertRaisesRegex(carry.CarryForwardError, "log_append_invalid"):
                carry.read_log_append(second)
            self.assertEqual(sample.call_count, 1)

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
                "files": [
                    {
                        "path": item["path"],
                        "exists": False,
                        "appended_text": "",
                        "end_size": 0,
                        "end_sha256": hashlib.sha256(b"").hexdigest(),
                    }
                    for item in previous["files"]
                ],
                "roots": copy.deepcopy(previous["roots"]),
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
            with worker.open("a") as output:
                output.write(
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
            same_request = copy.deepcopy(request)
            same_request["container_before"] = {
                "container_id": "new-worker",
                "status": "exited",
                "health": None,
                "started_at": "2026-09-19T00:00:00Z",
            }
            same_proof = carry._same_container_worker_startup_proof(
                same_request,
                profile,
                profile["candidate_image_ids_by_service"]["worker"],
                profile["candidate_profiles"]["worker"],
                ["2" * 32],
            )
            self.assertEqual(same_proof["container_id"], "new-worker")
            reused = copy.deepcopy(same_request)
            with self.assertRaisesRegex(
                carry.CarryForwardError, "group2_reboot_startup_invalid"
            ):
                carry._same_container_worker_startup_proof(
                    reused,
                    profile,
                    profile["candidate_image_ids_by_service"]["worker"],
                    profile["candidate_profiles"]["worker"],
                    [nonce],
                )
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


class Group2StartingReconcileTests(unittest.TestCase):
    def _request(self):
        failed_manifest = group2_manifest()
        failed_sha = failed_manifest["to_sha"]
        tool_sha = "d" * 40
        manifest_id = failed_manifest["manifest_id"]
        prior_id = "2" * 32
        h = lambda value: hashlib.sha256(value).hexdigest()
        source_scope = copy.deepcopy(group2_scope()["final_source"])
        source_scope["to_sha"] = tool_sha
        entries = source_scope["entries"]
        source_scope["tree_digest"] = carry.digest(
            {
                "from_tree": source_scope["from_tree"],
                "to_tree": source_scope["to_tree"],
                "entries": entries,
            }
        )
        controller_files = {
            name: h(name.encode()) for name in carry.GROUP2_CONTROLLER_FILES
        }
        review_entries = [
            item for item in entries if item["path"] in carry.GROUP2_CONTROLLER_PATHS
        ]
        review_record = {
            "schema": "group2-reconcile-tool-review-v1",
            "status": "APPROVED",
            "head_sha": tool_sha,
            "source_scope_digest": carry.digest(source_scope),
            "controller_files": controller_files,
            "entries": review_entries,
        }
        review_raw = (
            "review\n```json\n"
            + json.dumps(review_record, sort_keys=True)
            + "\n```\n"
        ).encode()
        embedded = lambda raw: {
            "sha256": h(raw),
            "content_b64": __import__("base64").b64encode(raw).decode(),
        }
        file_record = lambda raw: {"exists": True, **embedded(raw)}
        prior_manifest = copy.deepcopy(group2_manifest())
        prior_manifest["manifest_id"] = prior_id
        prior_manifest["manifest_digest"] = carry.digest(
            carry.manifest_payload(prior_manifest)
        )
        prior_manifest_raw = carry.canonical_bytes(prior_manifest)
        prior_images = {
            f"dlr-preview-{service}:{carry.GROUP2_FROM_SHA}": f"sha256:{index:064x}"
            for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
        }
        prior_state = {
            "sha": carry.GROUP2_FROM_SHA,
            "schema": "0040_issue152_dispositions",
            "images": prior_images,
            "backup": "/private/backup",
            "carry_forward": {
                "manifest_id": prior_id,
                "manifest_digest": prior_manifest["manifest_digest"],
                "selection_count": len(
                    prior_manifest["responsibilities"]["executions"]
                ),
            },
        }
        prior_probe = {
            "execution_id": 1,
            "status": "succeeded",
            "workspace_cleanup_status": "completed",
        }
        prior_receipt_raw = carry.canonical_bytes(
            {
                "schema": "0040_issue152_dispositions",
                "images": prior_images,
                "probe": prior_probe,
                "backup": "/private/backup",
                "carry_forward": {
                    "manifest_id": prior_id,
                    "manifest_digest": prior_manifest["manifest_digest"],
                    "selection_count": len(
                        prior_manifest["responsibilities"]["executions"]
                    ),
                },
            }
        )
        prior_manifest_name = f"carry-forward/manifests/{prior_id}.json"
        prior_consumed_name = f"carry-forward/consumed/{prior_id}.json"
        installed_files = failed_manifest["review_scope"]["controller_files"]["files"]
        jobs = [
            {
                "name": name,
                "id": index,
                "conclusion": "success",
                "status": "completed",
                "run_id": 9,
                "run_attempt": 1,
                "head_sha": tool_sha,
            }
            for index, name in enumerate(
                ("backend", "compose-smoke", "local-preview", "web"), 1
            )
        ]
        binding = {
            "head_sha": tool_sha,
            "run_id": 9,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "jobs": [
                {key: job[key] for key in ("name", "id", "conclusion")}
                for job in jobs
            ],
        }
        parsed = {
            "failed-manifest.json": failed_manifest,
            "first-startup.json": {
                "schema": "group2-first-startup-evidence-v1",
                "check_raw": {},
                "log_read_diagnostic": {},
                "actual_results": {},
                "db": {},
                "files": {},
                "legacy_assets": {},
            },
            "prior-success.json": {
                "schema": "group2-prior-success-evidence-v1",
                "host": {
                    "state.json": file_record(carry.canonical_bytes(prior_state)),
                    prior_manifest_name: {"exists": False},
                    prior_consumed_name: file_record(prior_manifest_raw),
                },
                "vm": {
                    prior_manifest_name: file_record(prior_manifest_raw),
                    prior_consumed_name: {"exists": False},
                    f"releases/{carry.GROUP2_FROM_SHA}/images.json": file_record(carry.canonical_bytes(prior_images)),
                    f"releases/{carry.GROUP2_FROM_SHA}/probe.json": file_record(carry.canonical_bytes(prior_probe)),
                    f"releases/{carry.GROUP2_FROM_SHA}/receipt.json": file_record(prior_receipt_raw),
                    f"releases/{carry.GROUP2_FROM_SHA}/schema": file_record(b"0040_issue152_dispositions\n"),
                },
            },
            "authority.json": {
                "schema": "group2-incident-authority-v1",
                "snapshot": {
                    "enabled": False,
                    "selected_pr": 161,
                    "state": prior_state,
                    "carry_reference": {"manifest_id": manifest_id, "manifest_digest": failed_manifest["manifest_digest"]},
                    "attention": {"phase": "switching", "candidate": {"sha": failed_sha}},
                    "transaction": {"phase": "starting", "sha": failed_sha},
                    "installed_files": installed_files,
                },
                "host_authority": {},
                "vm_inventory": {
                    "files": {
                        name: {
                            "exists": True,
                            "kind": "file",
                            "sha256": h(name.encode()),
                        }
                        for name in (
                            "deploy.sh",
                            "carry_forward.py",
                            "verify.py",
                            "assets.py",
                        )
                    },
                    "current_kernel": {},
                },
            },
            "platform.json": {
                "schema": "group2-incident-platform-v1",
                "images": {
                    "prior": {
                        service: {
                            "Id": prior_images[f"dlr-preview-{service}:{carry.GROUP2_FROM_SHA}"],
                            **(
                                {
                                    "postgres_version": "postgres (PostgreSQL) 16.15",
                                    "postgres_env_versions": ["PG_MAJOR=16", "PG_VERSION=16.15"],
                                }
                                if service == "postgres"
                                else {}
                            ),
                        }
                        for service in ("postgres", "control", "worker", "web")
                    },
                    "candidate": {
                        "postgres": {
                            "Id": "sha256:" + "9" * 64,
                            "postgres_version": "postgres (PostgreSQL) 16.15",
                            "postgres_env_versions": ["PG_MAJOR=16", "PG_VERSION=16.15"],
                        }
                    },
                },
                "postgres_format": {
                    "data_pg_version": "16",
                    "current_server_version": "16.15",
                    "prior_binary_version": "postgres (PostgreSQL) 16.15",
                    "candidate_binary_version": "postgres (PostgreSQL) 16.15",
                    "claim": "VERSION_AND_IMAGE_INSPECTION_ONLY_NO_ROLLBACK_OR_RESTORE",
                },
                "source_files": {
                    name: embedded(name.encode())
                    for name in (
                        "old-postgres.Dockerfile",
                        "candidate-postgres.Dockerfile",
                        "old-postgres-entrypoint.sh",
                        "candidate-postgres-entrypoint.sh",
                    )
                },
            },
            "source-review.json": {
                "schema": "group2-reconcile-source-review-v1",
                "status": "APPROVED",
                "tool_sha": tool_sha,
                "source_scope": source_scope,
                "controller_files": controller_files,
                "inherited_reviews": [
                    {"name": name, **embedded(str(index).encode())}
                    for index, name in enumerate(carry.GROUP2_INHERITED_REVIEW_HASHES)
                ],
                "tool_review": {**embedded(review_raw), "entries": review_entries},
            },
            "ci.json": {
                "schema": "group2-reconcile-ci-v1",
                "binding": binding,
                "raw": {
                    "run": {
                        "head_sha": tool_sha,
                        "id": 9,
                        "run_attempt": 1,
                        "path": ".github/workflows/ci.yml",
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    "jobs": {"total_count": 4, "jobs": jobs},
                },
            },
        }
        artifacts = {name: carry.canonical_bytes(value) for name, value in parsed.items()}
        request = {
            "schema": "group2-starting-reconcile-request-v1",
            "mode": carry.GROUP2_MODE,
            "action": "restore-prior-software",
            "incident_id": "e" * 32,
            "repo": "owner/repo",
            "pr": 161,
            "failed": {
                "sha": failed_sha,
                "manifest_id": manifest_id,
                "manifest_digest": failed_manifest["manifest_digest"],
                "manifest_sha256": h(artifacts["failed-manifest.json"]),
                "scope_digest": failed_manifest["review_scope_digest"],
                "installed_controller_files_digest": carry.digest(installed_files),
                "initial_evidence_digest": carry.digest(
                    {
                        name: h(artifacts[name])
                        for name in (
                            "failed-manifest.json",
                            "first-startup.json",
                            "prior-success.json",
                            "authority.json",
                            "platform.json",
                        )
                    }
                ),
            },
            "prior": {
                "sha": carry.GROUP2_FROM_SHA,
                "manifest_id": prior_id,
                "manifest_digest": prior_manifest["manifest_digest"],
                "receipt_sha256": h(prior_receipt_raw),
                "consumed_sha256": h(prior_manifest_raw),
                "images_digest": carry.digest(prior_images),
                "schema": "0040_issue152_dispositions",
            },
            "tool": {
                "sha": tool_sha,
                "controller_files": {
                    name: h(name.encode()) for name in carry.GROUP2_CONTROLLER_FILES
                },
                "source_scope_digest": carry.digest(source_scope),
                "review_report_sha256": h(artifacts["source-review.json"]),
                "ci_evidence_sha256": h(artifacts["ci.json"]),
            },
            "restore": {
                "up": ["postgres", "control", "worker", "web"],
                "stop_only": ["account-web"],
                "retain": ["rabbitmq"],
                "storage_identity_digest": "8" * 64,
                "account_policy": "stop-current-candidate-preserve-binding",
            },
            "evidence_files": {name: h(value) for name, value in artifacts.items()},
        }
        request["request_digest"] = carry.digest(request)
        approval = {
            "schema": "group2-starting-reconcile-approval-v1",
            "status": "USER_APPROVED",
            "request_digest": request["request_digest"],
            "tool_sha": tool_sha,
            "actions": list(carry.GROUP2_RECONCILE_ACTIONS),
            "user_reply": "批准本事故专用恢复动作",
            "user_record_sha256": "c" * 64,
        }
        return request, approval, artifacts

    def _complete_reconcile_case(self, root):
        request, approval, artifacts = self._request()
        parsed = {name: json.loads(value) for name, value in artifacts.items()}
        manifest = parsed["failed-manifest.json"]

        runtime = root / "runtime"
        journal = root / "journal"
        runtime.mkdir(mode=0o711)
        journal.mkdir(mode=0o700)
        for directory in (runtime, journal):
            (directory / ".dlr-instance.lock").touch(mode=0o600)
        (runtime / "attempt-journal").mkdir(mode=0o700)
        (runtime / "attempt-journal" / ".dlr-instance.lock").touch(mode=0o600)
        (runtime / "workspaces").mkdir(mode=0o700)
        (runtime / "version-cache").mkdir(mode=0o711)
        (runtime / "version-cache" / "entries").mkdir(mode=0o711)
        (runtime / "version-cache" / ".dlr-cache-reservations.json").write_text("{}")
        (runtime / "version-cache" / ".dlr-cache-reservations.json").chmod(0o600)
        (runtime / "version-cache" / ".dlr-cache-reservations.lock").touch(mode=0o644)
        (journal / "sandbox-recovery").mkdir(mode=0o700)
        files = carry.capture_files(runtime, journal)
        storage_identity = [
            {"service": "worker", "type": "volume", "source": "runtime",
             "destination": "/var/lib/dlr/runtime", "read_only": False},
            {"service": "worker", "type": "volume", "source": "journal",
             "destination": "/var/lib/dlr/journal", "read_only": False},
        ]
        manifest["storage_identity"] = storage_identity

        profile = Group2RuntimeTests()._profile(root / "logs" / "worker" / "worker.log")
        prior_images = {
            f"dlr-preview-{service}:{carry.GROUP2_FROM_SHA}": f"sha256:{index:064x}"
            for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
        }
        candidate_images = {
            service: f"sha256:{index + 10:064x}"
            for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
        }
        profile["from_sha"] = carry.GROUP2_FROM_SHA
        profile["to_sha"] = manifest["to_sha"]
        profile["candidate_image_ids_by_service"].update(
            {
                "control": candidate_images["control"],
                "worker": candidate_images["worker"],
                "web": candidate_images["web"],
                "account-web": candidate_images["web"],
            }
        )
        profile["candidate_web_image_id"] = candidate_images["web"]
        for service in profile["old_containers"]:
            image_service = "web" if service == "account-web" else service
            profile["old_containers"][service]["image_id"] = prior_images[
                f"dlr-preview-{image_service}:{carry.GROUP2_FROM_SHA}"
            ]
        profile["profile_digest"] = carry.digest(
            {key: value for key, value in profile.items() if key != "profile_digest"}
        )
        baseline = carry.capture_log_prefix(profile)

        def append(previous):
            return carry.read_log_append(previous)

        original_logs = []
        previous = baseline
        for _ in range(5):
            previous = append(previous)
            original_logs.append(previous)
        first_before = previous

        def startup(previous, nonce, image, container_id, service_profile):
            start = previous["observed_after_ns"]
            started = max(start, __import__("time").time_ns())
            receipt = FileEvidenceTests._startup_proof(
                FileEvidenceTests(), start, started + 1_000_000
            )["preflight_receipt"]
            receipt["cgroup_name"] = f"dlr-preflight-{nonce}"
            receipt["cleanup"]["cgroup_name"] = receipt["cgroup_name"]
            receipt["namespace_identity"] = {
                "boot_id": "boot",
                "parent_device": 1,
                "parent_inode": 2,
                "root_device": 1,
                "root_inode": 30 if nonce.startswith("1") else 40,
            }
            worker = Path(
                next(path for path in profile["log_files"] if path.endswith("/worker/worker.log"))
            )
            with worker.open("a") as output:
                output.write(
                    "sandbox preflight receipt: "
                    + json.dumps(receipt, sort_keys=True)
                    + "\nsandbox preflight passed; rabbitmq execution gate=True\n"
                )
            if nonce.startswith("2"):
                control = Path(
                    next(
                        path
                        for path in profile["log_files"]
                        if path.endswith("/control/control.log")
                    )
                )
                with control.open("a") as output:
                    output.write(
                        'x 127.0.0.1:5000 - "POST /api/workers/register '
                        'HTTP/1.1" 200\n'
                    )
            after = append(previous)
            end = after["observed_after_ns"]
            timestamp = __import__("datetime").datetime.fromtimestamp(
                started / 1_000_000_000, __import__("datetime").timezone.utc
            ).isoformat(timespec="microseconds").replace("+00:00", "Z")
            container = {
                "container_id": container_id,
                "image_id": image,
                "status": "running",
                "health": "healthy",
                "started_at": timestamp,
                "restart_count": 0,
                "command": service_profile["effective_command"],
                "labels": {
                    "com.docker.compose.project": profile["project"],
                    "com.docker.compose.service": "worker",
                },
                "port_bindings": {},
                "mounts": service_profile["mounts"],
                "networks": service_profile["networks"],
            }
            return after, container, start, end

        first_after, first_worker, first_start, first_end = startup(
            first_before, "1" * 32, candidate_images["worker"],
            "candidate-worker", profile["candidate_profiles"]["worker"],
        )
        first_request = {
            "mode": carry.GROUP2_MODE,
            "operation": "startup-proof",
            "profile": profile,
            "logs_before": first_before,
            "logs_after": first_after,
            "container_before": profile["old_containers"]["worker"],
            "container_after": first_worker,
            "window_start_ns": first_start,
            "window_end_ns": first_end,
        }
        first_proof = carry._startup_proof(first_request)
        os.utime(runtime, ns=(first_start, first_start))
        recovery = journal / "sandbox-recovery"
        os.utime(recovery, ns=(first_start + 1, first_start + 1))
        startup_files = carry.capture_files(runtime, journal)
        first_files_result = carry.compare_group2_startup_files(
            files, startup_files, first_proof
        )
        db = {
            "projection": manifest["old_runtime_projection"],
            "responsibilities": manifest["responsibilities"],
            "protected_rows": manifest["protected_rows"],
            "asset_projection": manifest["asset_projection"],
            "schema_shape": manifest["schema_shape"],
            "schema_inventory": manifest["schema_inventory"],
        }
        reference = manifest["review_scope"]["preservation_reference"]
        reference["snapshot"]["db"] = db
        reference["snapshot"]["files"] = files
        reference["snapshot_digest"] = carry.digest(reference["snapshot"])
        manifest["review_scope"]["image_binding"] = {
            "old_image_ids": prior_images,
            "candidate_image_ids": {
                f"dlr-{name}:{manifest['to_sha']}": value
                for name, value in candidate_images.items()
            },
        }
        manifest["review_scope"]["preservation_reference"] = reference
        manifest["review_scope"]["scope_digest"] = carry.digest(
            {
                key: value
                for key, value in manifest["review_scope"].items()
                if key != "scope_digest"
            }
        )
        manifest["review_scope_digest"] = manifest["review_scope"]["scope_digest"]
        manifest["preservation_reference_digest"] = reference["snapshot_digest"]
        manifest["account_entry"] = profile
        manifest["log_evidence"] = baseline
        manifest["file_evidence"] = files
        manifest["old_image_ids"] = prior_images
        manifest["candidate_image_ids"] = manifest["review_scope"]["image_binding"][
            "candidate_image_ids"
        ]
        manifest["manifest_digest"] = carry.digest(carry.manifest_payload(manifest))
        parsed["failed-manifest.json"] = manifest
        check = {"log-baseline.json": baseline}
        for name, logs in zip(
            ("preflight", "control-stopped", "stopped", "after-backup", "after-migration"),
            original_logs,
        ):
            check[f"{name}/log-request.json"] = {
                "baseline": baseline if name == "preflight" else original_logs[
                    ("preflight", "control-stopped", "stopped", "after-backup", "after-migration").index(name) - 1
                ]
            }
            check[f"{name}/log.json"] = {"log_evidence": logs}
            check[f"{name}/db.json"] = db
            check[f"{name}/files.json"] = files
        check.update(
            {
                "group2/account-check.json": {
                    "account_check": {"containers": {"worker": first_worker}}
                },
                "group2/log-before-start.json": {"log_evidence": first_before},
                "group2/log-after-start-request.json": {"baseline": first_before},
                "group2/start-window.json": {
                    "window_start_ns": first_start,
                    "window_end_ns": first_end,
                },
            }
        )
        parsed["first-startup.json"] = {
            "schema": "group2-first-startup-evidence-v1",
            "check_raw": check,
            "log_read_diagnostic": {"log_append": first_after},
            "actual_results": {
                "startup_reconstruction": {
                    "result": "DERIVED_FROM_LATER_DIAGNOSTIC_ONLY",
                    "proof": first_proof,
                }
            },
            "db": db,
            "files": startup_files,
            "legacy_assets": {
                name: {
                    "columns": item["columns"],
                    "rows": [carry.digest(row) for row in item["rows"]],
                }
                for name, item in db["asset_projection"].items()
            },
        }

        def container(service, image, container_id, status="running"):
            service_profile = profile["old_profiles"].get(service, profile["old_profiles"]["web"])
            return {
                "container_id": container_id,
                "image_id": image,
                "status": status,
                "health": "healthy" if status == "running" else None,
                "started_at": "2026-09-20T00:00:00Z",
                "restart_count": 0,
                "command": service_profile["effective_command"],
                "labels": {
                    "com.docker.compose.project": profile["project"],
                    "com.docker.compose.service": service,
                },
                "port_bindings": (
                    {} if service in {"postgres", "rabbitmq"}
                    else carry._profile_port_bindings(service_profile)
                ),
                "mounts": service_profile["mounts"],
                "networks": service_profile["networks"],
            }

        containers = {
            "postgres": container("postgres", candidate_images["postgres"], "candidate-postgres"),
            "rabbitmq": container("rabbitmq", "rabbit-image", "rabbit"),
            "control": container("control", candidate_images["control"], "candidate-control"),
            "worker": first_worker,
            "web": container("web", candidate_images["web"], "candidate-web"),
            "account-web": container("account-web", candidate_images["web"], "candidate-account"),
        }
        authority = {
            "container_id": first_worker["container_id"],
            "image_id": first_worker["image_id"],
            "started_at": first_worker["started_at"],
            "pid": 101,
            "pid_starttime": "10",
            "mount_namespace": "mnt:[1]",
            "cgroup_namespace": "cgroup:[1]",
            "parent_device": 1,
            "parent_inode": 2,
            "root_device": 1,
            "root_inode": 20,
            "labels": first_worker["labels"],
            "runtime_config": {
                "user": "0", "runtime_root": "/var/lib/dlr/runtime",
                "journal_root": "/var/lib/dlr/journal",
                "attempt_journal_root": "/var/lib/dlr/runtime/attempt-journal",
                "cgroup_path": "/run/dlr-cgroup",
            },
            "volumes": {
                "runtime": {"name": "runtime", "device": os.makedev(1, 2),
                            "inode": 101},
                "journal": {"name": "journal", "device": os.makedev(1, 2),
                            "inode": 102},
            },
        }
        child = lambda pid: {
            "populated": 1, "process_count": 1,
            "process_digest": carry.digest([pid]), "device": 1, "inode": pid,
        }
        worker_root = lambda inode: {
            "populated": 1, "process_count": 0,
            "process_digest": carry.digest([]), "device": 1, "inode": inode,
        }
        active_kernel = {
            "boot_id": "boot", "unit": "dlr-test.service",
            "control_group": "/system.slice/dlr-test.service", "keeper_pid": 7,
            "keeper_starttime": "7", "description": "DataLinkRuntime Sandbox dlr-test.service CPU=100% Memory=1G",
            "parent_device": 1, "parent_inode": 2,
            "children": {
                "agent": child(7),
                first_worker["container_id"]: worker_root(20),
                f"{first_worker['container_id']}/agent": child(101),
            },
            "old_worker_authority": authority, "namespace_evidence": None,
            "retired_markers": [],
        }
        parsed["authority.json"]["vm_inventory"]["current_kernel"] = active_kernel
        parsed["authority.json"]["snapshot"]["carry_reference"] = {
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["manifest_digest"],
        }
        parsed["platform.json"]["images"]["candidate"].update(
            {name: {"Id": image} for name, image in candidate_images.items() if name != "postgres"}
        )
        parsed["platform.json"]["images"]["candidate"]["postgres"]["Id"] = candidate_images["postgres"]

        artifacts = {name: carry.canonical_bytes(value) for name, value in parsed.items()}
        request["failed"].update(
            {
                "manifest_digest": manifest["manifest_digest"],
                "manifest_sha256": hashlib.sha256(artifacts["failed-manifest.json"]).hexdigest(),
                "scope_digest": manifest["review_scope_digest"],
                "initial_evidence_digest": carry.digest(
                    {
                        name: hashlib.sha256(artifacts[name]).hexdigest()
                        for name in (
                            "failed-manifest.json", "first-startup.json", "prior-success.json",
                            "authority.json", "platform.json",
                        )
                    }
                ),
            }
        )
        request["restore"]["storage_identity_digest"] = carry.digest(storage_identity)
        request["evidence_files"] = {
            name: hashlib.sha256(value).hexdigest() for name, value in artifacts.items()
        }
        request["request_digest"] = carry.digest(
            {key: value for key, value in request.items() if key != "request_digest"}
        )
        approval["request_digest"] = request["request_digest"]
        inherited = {
            item["name"]: item["sha256"]
            for item in parsed["source-review.json"]["inherited_reviews"]
        }
        with mock.patch.object(carry, "GROUP2_INHERITED_REVIEW_HASHES", inherited):
            validated = carry.validate_group2_reconcile_request(request, approval, artifacts)

        log_preflight = append(first_after)
        log_control = append(log_preflight)
        log_apps = append(log_control)
        log_pg = append(log_apps)
        idle_kernel = copy.deepcopy(active_kernel)
        idle_kernel["children"] = {"agent": active_kernel["children"]["agent"]}
        idle_kernel["namespace_evidence"] = {
            "task_count": 0, "namespace_count": 0, "mountinfo_bytes": 0,
            "fd_entries": [], "related_count": 0, "pin_count": 0,
            "target_digest": carry.digest({"related": [], "pins": []}),
        }
        control_containers = copy.deepcopy(containers)
        control_containers["control"]["status"] = "exited"
        apps_containers = copy.deepcopy(control_containers)
        for service in ("worker", "web", "account-web"):
            apps_containers[service]["status"] = "exited"
            apps_containers[service]["health"] = None
        pg_containers = copy.deepcopy(apps_containers)
        pg_containers["postgres"] = container(
            "postgres", prior_images[f"dlr-preview-postgres:{carry.GROUP2_FROM_SHA}"],
            "restored-postgres",
        )
        second_after, final_worker, second_start, second_end = startup(
            log_pg, "2" * 32, prior_images[f"dlr-preview-worker:{carry.GROUP2_FROM_SHA}"],
            "restored-worker", profile["old_profiles"]["worker"],
        )
        final_containers = copy.deepcopy(pg_containers)
        for service in ("control", "worker", "web"):
            image = prior_images[f"dlr-preview-{service}:{carry.GROUP2_FROM_SHA}"]
            final_containers[service] = (
                final_worker if service == "worker" else container(service, image, f"restored-{service}")
            )
        final_authority = copy.deepcopy(authority)
        final_authority.update(
            {
                "container_id": final_worker["container_id"],
                "image_id": final_worker["image_id"],
                "started_at": final_worker["started_at"],
                "pid": 202, "pid_starttime": "20",
                "labels": final_worker["labels"],
                "root_device": 1, "root_inode": 40,
            }
        )
        final_kernel = copy.deepcopy(active_kernel)
        final_kernel["old_worker_authority"] = final_authority
        final_kernel["children"] = {
            "agent": child(7), final_worker["container_id"]: worker_root(40),
            f"{final_worker['container_id']}/agent": child(202),
        }
        final_kernel["namespace_evidence"] = None

        def stage(logs, kernel, stage_containers, start, end):
            return {
                "db": db, "files": startup_files, "logs": logs, "kernel": kernel,
                "containers": stage_containers, "storage": storage_identity,
                "window_start_ns": start, "window_end_ns": end,
            }

        t0 = first_after["observed_after_ns"] + 1
        preflight = stage(log_preflight, active_kernel, containers, t0, log_preflight["observed_after_ns"])
        control_stage = stage(log_control, active_kernel, control_containers, preflight["window_end_ns"], log_control["observed_after_ns"])
        apps_stage = stage(log_apps, idle_kernel, apps_containers, control_stage["window_end_ns"], log_apps["observed_after_ns"])
        pg_stage = stage(log_pg, idle_kernel, pg_containers, apps_stage["window_end_ns"], log_pg["observed_after_ns"])
        restored_stage = stage(second_after, final_kernel, final_containers, second_end, second_end)
        second_request = {
            "mode": carry.GROUP2_MODE, "operation": "startup-proof", "profile": profile,
            "logs_before": log_pg, "logs_after": second_after,
            "container_before": pg_containers["worker"], "container_after": final_worker,
            "window_start_ns": second_start, "window_end_ns": second_end,
        }
        second_proof = carry._incident_startup_proof(second_request)
        final_files = copy.deepcopy(startup_files)
        final_files["runtime"]["root"]["mtime_ns"] = second_start
        recovery_item = next(
            item
            for item in final_files["journal"]["entries"]
            if item["path"] == "sandbox-recovery"
        )
        recovery_item["mtime_ns"] = second_start + 1
        final_files["journal"]["digest"] = carry.digest(
            final_files["journal"]["entries"]
        )
        restored_stage["files"] = final_files
        evidence = {
            "preflight": preflight,
            "stopped": {"control": control_stage, "apps": apps_stage},
            "restored_postgres": pg_stage,
            "restore_startup": {"request": second_request, "proof": second_proof},
            "restored": restored_stage,
            "account": {"before": containers["account-web"], "after": final_containers["account-web"]},
            "images": {
                "prior": prior_images,
                "restored": {
                    service: prior_images[f"dlr-preview-{service}:{carry.GROUP2_FROM_SHA}"]
                    for service in ("postgres", "control", "worker", "web")
                },
                "rabbitmq_before": "rabbit-image", "rabbitmq_after": "rabbit-image",
                "postgres": {
                    "preflight": {
                        "schema": request["prior"]["schema"],
                        "binary_version": "postgres (PostgreSQL) 16.15",
                        "server_version": "16.15", "data_pg_version": "16",
                    },
                    "restored": {
                        "schema": request["prior"]["schema"],
                        "binary_version": "postgres (PostgreSQL) 16.15",
                        "server_version": "16.15", "data_pg_version": "16",
                    },
                },
            },
            "storage": carry.digest(storage_identity),
            "token_health": {"status": 200, "database": True},
        }
        return request, approval, artifacts, validated, evidence, reference, first_files_result

    def test_raw_capture_reaches_stage_gate_after_allowed_startup_file_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            (
                _request,
                _approval,
                _artifacts,
                validated,
                evidence,
                _reference,
                _first_files_result,
            ) = self._complete_reconcile_case(root)
            manifest = validated["artifacts"]["failed-manifest.json"]
            current_db = evidence["preflight"]["db"]
            current_files = evidence["preflight"]["files"]
            self.assertNotEqual(manifest["file_evidence"], current_files)

            baseline = root / "failed-manifest.json"
            ids = root / "ids.json"
            carry.write_private(baseline, manifest)
            carry.write_private(ids, manifest["selection"])

            def inspect_database(*_args, **_kwargs):
                value = copy.deepcopy(current_db)
                value["_credential_hashes"] = {}
                value["_attempt_statuses"] = {}
                return value

            def arguments(*, baseline_path, ids_path, stem):
                return SimpleNamespace(
                    baseline=baseline_path,
                    ids=ids_path,
                    mode=carry.GROUP2_MODE,
                    schema_phase="before",
                    runtime_root=root / "runtime",
                    journal_root=root / "journal",
                    material_root=[],
                    expected_uid=None,
                    db_output=root / f"{stem}-db.json",
                    files_output=root / f"{stem}-files.json",
                )

            with (
                mock.patch.object(
                    carry, "inspect_database", side_effect=inspect_database
                ),
                mock.patch.object(
                    carry,
                    "capture_files",
                    side_effect=lambda *_args, **_kwargs: copy.deepcopy(
                        current_files
                    ),
                ),
                self.assertRaisesRegex(
                    carry.CarryForwardError, "file_evidence_changed"
                ),
            ):
                carry._command_capture_state(
                    arguments(baseline_path=baseline, ids_path=None, stem="strict")
                )

            with (
                mock.patch.object(
                    carry, "inspect_database", side_effect=inspect_database
                ),
                mock.patch.object(
                    carry,
                    "capture_files",
                    side_effect=lambda *_args, **_kwargs: copy.deepcopy(
                        current_files
                    ),
                ),
            ):
                result = carry._command_capture_state(
                    arguments(baseline_path=None, ids_path=ids, stem="raw")
                )
            captured_db = carry.read_private(root / "raw-db.json")
            captured_files = carry.read_private(root / "raw-files.json")
            self.assertEqual(result["code"], "state_ok")
            self.assertEqual(captured_db, current_db)
            self.assertEqual(captured_files, current_files)

            current = copy.deepcopy(evidence["stopped"]["control"])
            current["db"] = captured_db
            current["files"] = captured_files
            self.assertEqual(
                carry.validate_group2_reconcile_transition(
                    evidence["preflight"],
                    current,
                    kernel_baseline=evidence["preflight"]["kernel"],
                    require_idle=False,
                ),
                current,
            )
            for label, mutate in (
                (
                    "database",
                    lambda value: value["db"].__setitem__("projection", {}),
                ),
                (
                    "files",
                    lambda value: value["files"]["runtime"]["root"].__setitem__(
                        "inode", 999
                    ),
                ),
            ):
                changed = copy.deepcopy(current)
                mutate(changed)
                with self.subTest(label=label), self.assertRaises(
                    carry.CarryForwardError
                ):
                    carry.validate_group2_reconcile_transition(
                        evidence["preflight"],
                        changed,
                        kernel_baseline=evidence["preflight"]["kernel"],
                        require_idle=False,
                    )

    def test_restored_kernel_projects_real_compose_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                request,
                _approval,
                _artifacts,
                validated,
                evidence,
                _reference,
                _first_files_result,
            ) = self._complete_reconcile_case(Path(temporary))
            worker = evidence["restored"]["containers"]["worker"]
            worker["labels"] = {
                **worker["labels"],
                "ao.session": "synthetic",
                "com.docker.compose.config-hash": "synthetic",
                "com.docker.compose.container-number": "1",
                "com.docker.compose.depends_on": "synthetic",
                "com.docker.compose.image": "sha256:synthetic",
                "com.docker.compose.oneoff": "False",
                "com.docker.compose.project.config_files": "/synthetic/compose.yml",
                "com.docker.compose.project.environment_file": "/synthetic/.env",
                "com.docker.compose.project.working_dir": "/synthetic",
                "com.docker.compose.replace": "synthetic",
                "com.docker.compose.version": "2.synthetic",
            }
            self.assertEqual(len(worker["labels"]), 13)
            evidence["restore_startup"]["request"]["container_after"] = copy.deepcopy(
                worker
            )
            evidence["restore_startup"]["proof"] = carry._incident_startup_proof(
                evidence["restore_startup"]["request"]
            )
            self.assertEqual(
                carry.validate_group2_reconcile_result(
                    request, evidence, validated
                )["schema"],
                "group2-software-reconcile-v1",
            )

            for label_key in (
                "com.docker.compose.project",
                "com.docker.compose.service",
            ):
                changed = copy.deepcopy(evidence)
                changed["restored"]["kernel"]["old_worker_authority"][
                    "labels"
                ][label_key] = "wrong"
                with self.subTest(label_key=label_key), self.assertRaisesRegex(
                    carry.CarryForwardError, "kernel_changed"
                ):
                    carry.validate_group2_reconcile_result(
                        request, changed, validated
                    )

    def test_partial_finalize_result_recomputes_history_and_current_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                _request,
                _approval,
                _artifacts,
                original_validated,
                restored_evidence,
                _reference,
                _first_files_result,
            ) = self._complete_reconcile_case(Path(temporary))
            original = carry._group2_reconcile_originals(original_validated)
            diagnostic = copy.deepcopy(restored_evidence["restored"])
            diagnostic["logs"] = carry.combine_group2_log_window(
                original["logs_after_startup"],
                restored_evidence["preflight"]["logs"],
                restored_evidence["stopped"]["control"]["logs"],
                restored_evidence["stopped"]["apps"]["logs"],
                restored_evidence["restored_postgres"]["logs"],
                restored_evidence["restored"]["logs"],
            )
            startup = restored_evidence["restore_startup"]

            def utc_text(value):
                import datetime
                seconds, nanoseconds = divmod(value, 1_000_000_000)
                base = datetime.datetime.fromtimestamp(
                    seconds, tz=datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S")
                return f"{base}.{nanoseconds:09d}+00:00"

            phase = {
                "schema": "group2-starting-reconcile-phase-v1",
                "incident_id": carry.GROUP2_PARTIAL_INCIDENT_ID,
                "phase": "restoring-apps",
            }
            context = {
                "validated_original": original_validated,
                "originals": original,
                "attempt": {
                    "started_at": utc_text(startup["request"]["window_start_ns"]),
                    "finished_at": utc_text(startup["request"]["window_end_ns"]),
                },
                "phase": {"phase": phase},
                "diagnostic": {"stage": diagnostic},
            }
            request = {
                "incident_id": carry.GROUP2_PARTIAL_INCIDENT_ID,
                "finalize_id": "f" * 32,
                "original_request_digest": carry.GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST,
                "request_digest": "e" * 64,
                "tool": {"sha": "d" * 40},
            }
            current = copy.deepcopy(diagnostic)
            current["logs"] = carry.read_log_append(diagnostic["logs"])
            current["window_start_ns"] = diagnostic["window_end_ns"]
            current["window_end_ns"] = current["window_start_ns"]

            def embedded(value):
                raw = carry.canonical_bytes(value)
                return {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_b64": __import__("base64").b64encode(raw).decode(),
                }

            snapshot = original_validated["artifacts"]["authority.json"]["snapshot"]
            config = {
                "enabled": False,
                "repo": original_validated["request"]["repo"],
                "pr": original_validated["request"]["pr"],
                "carry_forward": snapshot["carry_reference"],
            }
            host_files = {
                "config.json": embedded(config),
                "state.json": embedded(snapshot["state"]),
                "attention.json": embedded(snapshot["attention"]),
            }
            installed = snapshot["installed_files"]
            vm_installed = carry._reconcile_vm_installed_files(
                original_validated["artifacts"]["authority.json"]
            )
            prior = original_validated["artifacts"]["prior-success.json"]
            prior_small = {
                side: {
                    name: (
                        {"exists": True, "sha256": value["sha256"]}
                        if value.get("exists") is True else {"exists": False}
                    )
                    for name, value in prior[side].items()
                }
                for side in ("host", "vm")
            }
            authority = {
                "host_files": host_files,
                "host_installed": installed,
                "vm_files": {
                    "transaction.json": embedded(
                        {"phase": "starting", "sha": carry.GROUP2_PARTIAL_FAILED_SHA}
                    ),
                    "current-sha": {
                        "sha256": hashlib.sha256(
                            (carry.GROUP2_FROM_SHA + "\n").encode()
                        ).hexdigest(),
                        "content_b64": __import__("base64").b64encode(
                            (carry.GROUP2_FROM_SHA + "\n").encode()
                        ).decode(),
                    },
                    "phase.json": embedded(phase),
                },
                "vm_installed": vm_installed,
                "prior_success": prior_small,
            }
            context["diagnostic_host_state"] = {
                "state_sha256": host_files["state.json"]["sha256"],
                "attention_sha256": host_files["attention.json"]["sha256"],
                "installed": installed,
            }
            context["diagnostic_vm_state"] = {
                "installed": vm_installed,
                "current_sha": carry.GROUP2_FROM_SHA,
            }
            platform = original_validated["artifacts"]["platform.json"][
                "postgres_format"
            ]
            evidence = {
                "current": current,
                "authority": authority,
                "postgres": {
                    "schema": "0040_issue152_dispositions",
                    "binary_version": platform["prior_binary_version"],
                    "server_version": platform["current_server_version"],
                    "data_pg_version": platform["data_pg_version"],
                },
                "token_health": {"status": 200, "database": True},
            }
            validated = {
                "request": request,
                "approval": {},
                "user_record": {},
                "artifacts": {},
                "context": context,
            }
            with (
                mock.patch.object(
                    carry, "GROUP2_PARTIAL_NONCE", startup["proof"]["nonce"]
                ),
                mock.patch.object(
                    carry, "GROUP2_PARTIAL_WORKER_ID", startup["proof"]["container_id"]
                ),
            ):
                receipt = carry.validate_group2_partial_finalize_result(
                    validated, evidence
                )
                self.assertEqual(
                    receipt["result"], "finalized_observed_prior_software"
                )
                for label, mutate in (
                    (
                        "database",
                        lambda value: value["current"]["db"].__setitem__(
                            "projection", {}
                        ),
                    ),
                    (
                        "third file change",
                        lambda value: value["current"]["files"]["runtime"][
                            "root"
                        ].__setitem__("inode", 999),
                    ),
                    (
                        "authority label",
                        lambda value: value["current"]["kernel"][
                            "old_worker_authority"
                        ]["labels"].__setitem__("extra", "forbidden"),
                    ),
                    (
                        "prior success",
                        lambda value: next(
                            item
                            for item in value["authority"]["prior_success"]["vm"].values()
                            if item.get("exists") is True
                        ).__setitem__("sha256", "0" * 64),
                    ),
                ):
                    changed = copy.deepcopy(evidence)
                    mutate(changed)
                    with self.subTest(label=label), self.assertRaises(
                        carry.CarryForwardError
                    ):
                        carry.validate_group2_partial_finalize_result(
                            validated, changed
                        )

    def test_reconcile_request_is_closed_and_separately_approved(self):
        request, approval, artifacts = self._request()
        with mock.patch.object(carry, "_validate_reconcile_wrappers"):
            result = carry.validate_group2_reconcile_request(request, approval, artifacts)
        self.assertEqual(result["request"], request)
        for label, mutate, code in (
            ("missing approval", lambda r, a, x: a.clear(), "approval_invalid"),
            ("general approval", lambda r, a, x: a.__setitem__("schema", "general"), "approval_invalid"),
            ("wrong request", lambda r, a, x: a.__setitem__("request_digest", "0" * 64), "approval_invalid"),
            ("wrong tool", lambda r, a, x: a.__setitem__("tool_sha", "0" * 40), "approval_invalid"),
            ("wrong actions", lambda r, a, x: a["actions"].reverse(), "approval_invalid"),
            ("dirty payload", lambda r, a, x: x.__setitem__("ci.json", x["ci.json"] + b"\n"), "artifact_digest"),
            ("extra evidence", lambda r, a, x: x.__setitem__("extra.json", b"{}"), "artifact_invalid"),
            ("installed confused", lambda r, a, x: r["failed"].__setitem__("installed_controller_files_digest", "0" * 64), "request_digest"),
        ):
            changed_request = copy.deepcopy(request)
            changed_approval = copy.deepcopy(approval)
            changed_artifacts = copy.deepcopy(artifacts)
            mutate(changed_request, changed_approval, changed_artifacts)
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(carry.CarryForwardError, code),
                mock.patch.object(carry, "_validate_reconcile_wrappers"),
            ):
                carry.validate_group2_reconcile_request(
                    changed_request, changed_approval, changed_artifacts
                )

    def test_partial_finalize_tool_requires_both_reviews_and_exact_ci(self):
        request, _approval, artifacts = self._request()
        source = json.loads(artifacts["source-review.json"])
        ci = json.loads(artifacts["ci.json"])
        review = source["tool_review"]
        tool_raw = __import__("base64").b64decode(review["content_b64"])
        independent = {
            "schema": "group2-independent-review-v1",
            "status": "APPROVED",
            "reviewed_commit": request["tool"]["sha"],
            "source_kind": "git_commit",
            "coverage": [
                {
                    "path": item["path"],
                    "mode": item["new_mode"],
                    "blob_oid": item["new_oid"],
                    "sha256": hashlib.sha256(item["path"].encode()).hexdigest(),
                }
                for item in review["entries"]
            ],
            "blocking_findings": [],
        }
        raw = tool_raw + b"\n```json\n" + json.dumps(independent).encode() + b"\n```\n"
        review["sha256"] = hashlib.sha256(raw).hexdigest()
        review["content_b64"] = __import__("base64").b64encode(raw).decode()
        inherited = {
            item["name"]: item["sha256"] for item in source["inherited_reviews"]
        }

        def validate(source_value, ci_value):
            with mock.patch.object(
                carry, "GROUP2_INHERITED_REVIEW_HASHES", inherited
            ):
                return carry._validate_group2_partial_tool(
                    request["tool"], source_value, ci_value
                )

        validate(source, ci)

        for label, mutate in (
            (
                "missing independent",
                lambda value: value["tool_review"].__setitem__(
                    "content_b64",
                    __import__("base64").b64encode(tool_raw).decode(),
                ),
            ),
            (
                "coverage mode",
                lambda value: value["tool_review"]["entries"][0].__setitem__(
                    "new_mode", "100755"
                ),
            ),
        ):
            changed = copy.deepcopy(source)
            mutate(changed)
            if label == "missing independent":
                changed["tool_review"]["sha256"] = hashlib.sha256(
                    __import__("base64").b64decode(
                        changed["tool_review"]["content_b64"]
                    )
                ).hexdigest()
            with self.subTest(label=label), self.assertRaises(
                carry.CarryForwardError
            ):
                validate(changed, ci)
        changed_ci = copy.deepcopy(ci)
        changed_ci["raw"]["run"]["head_sha"] = "0" * 40
        with self.assertRaises(carry.CarryForwardError):
            validate(source, changed_ci)

    def test_partial_finalize_requires_new_bound_approval(self):
        raw_artifacts = {
            name: carry.canonical_bytes({"name": name})
            for name in carry.GROUP2_PARTIAL_FINALIZE_FILES
        }
        tool = {
            "sha": "d" * 40,
            "controller_files": {
                name: hashlib.sha256(name.encode()).hexdigest()
                for name in carry.GROUP2_CONTROLLER_FILES
            },
            "source_scope_digest": "1" * 64,
            "review_report_sha256": hashlib.sha256(
                raw_artifacts["source-review.json"]
            ).hexdigest(),
            "ci_evidence_sha256": hashlib.sha256(
                raw_artifacts["ci.json"]
            ).hexdigest(),
        }
        request = {
            "schema": "group2-partial-finalize-request-v1",
            "mode": carry.GROUP2_MODE,
            "action": "finalize-observed-prior-software",
            "incident_id": carry.GROUP2_PARTIAL_INCIDENT_ID,
            "finalize_id": "f" * 32,
            "repo": "owner/repo",
            "pr": 161,
            "original_request_digest": carry.GROUP2_PARTIAL_ORIGINAL_REQUEST_DIGEST,
            "tool": tool,
            "evidence_files": {
                name: hashlib.sha256(raw).hexdigest()
                for name, raw in raw_artifacts.items()
            },
        }
        request["request_digest"] = carry.digest(request)
        record = {
            "schema": "group2-partial-finalize-user-record-v1",
            "request_digest": request["request_digest"],
            "tool_sha": tool["sha"],
            "actions": carry.GROUP2_PARTIAL_FINALIZE_ACTIONS,
            "presented_request": " ".join(
                (
                    request["incident_id"], request["finalize_id"],
                    request["original_request_digest"], request["request_digest"],
                    tool["sha"], *carry.GROUP2_PARTIAL_FINALIZE_ACTIONS,
                    "historical raw unavailable outer_official_attempt",
                )
            ),
            "user_reply": "approved exact finalize",
        }
        user_record = carry.canonical_bytes(record)
        approval = {
            "schema": "group2-partial-finalize-approval-v1",
            "status": "USER_APPROVED",
            "request_digest": request["request_digest"],
            "tool_sha": tool["sha"],
            "actions": carry.GROUP2_PARTIAL_FINALIZE_ACTIONS,
            "user_reply": record["user_reply"],
            "user_record_sha256": hashlib.sha256(user_record).hexdigest(),
        }
        original_context = {
            "validated_original": {"request": {"repo": "owner/repo", "pr": 161}}
        }
        with (
            mock.patch.object(
                carry,
                "_validate_group2_partial_originals",
                return_value=original_context,
            ),
            mock.patch.object(carry, "_validate_group2_partial_tool"),
        ):
            validated = carry.validate_group2_partial_finalize_request(
                request, approval, user_record, raw_artifacts
            )
            self.assertEqual(validated["request"], request)
            empty_record = copy.deepcopy(record)
            empty_record["user_reply"] = ""
            empty_raw = carry.canonical_bytes(empty_record)
            empty_approval = copy.deepcopy(approval)
            empty_approval["user_reply"] = ""
            empty_approval["user_record_sha256"] = hashlib.sha256(
                empty_raw
            ).hexdigest()
            with self.assertRaises(carry.CarryForwardError):
                carry.validate_group2_partial_finalize_request(
                    request, empty_approval, empty_raw, raw_artifacts
                )
            for label, mutate in (
                (
                    "scope acceptance is not approval",
                    lambda value: value.__setitem__("status", "SCOPE_ACCEPTED"),
                ),
                (
                    "old approval schema",
                    lambda value: value.__setitem__(
                        "schema", "group2-starting-reconcile-approval-v1"
                    ),
                ),
                (
                    "wrong request",
                    lambda value: value.__setitem__("request_digest", "0" * 64),
                ),
            ):
                changed = copy.deepcopy(approval)
                mutate(changed)
                with self.subTest(label=label), self.assertRaises(
                    carry.CarryForwardError
                ):
                    carry.validate_group2_partial_finalize_request(
                        request, changed, user_record, raw_artifacts
                    )

    def test_partial_finalize_streaming_embedded_digest_binds_content(self):
        raw = (b"partial-finalize-evidence\n" * 300000) + b"tail"
        embedded = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content_b64": __import__("base64").b64encode(raw).decode(),
        }
        carry._validate_embedded_digest(
            embedded, embedded["sha256"], "partial_chain_invalid"
        )
        canonical_value = {
            "embedded": embedded, "escaped": "line\n\"quoted\"雪",
            "values": [None, True, False, 3, 1.5],
        }
        self.assertEqual(
            carry.streaming_digest(canonical_value), carry.digest(canonical_value)
        )
        changed = copy.deepcopy(embedded)
        changed["content_b64"] = changed["content_b64"][:-4] + "AAAA"
        with self.assertRaisesRegex(
            carry.CarryForwardError, "partial_chain_invalid"
        ):
            carry._validate_embedded_digest(
                changed, embedded["sha256"], "partial_chain_invalid"
            )

    def test_complete_reconcile_validators_bind_originals_stages_and_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            (
                request,
                approval,
                artifacts,
                validated,
                evidence,
                reference,
                _first_files_result,
            ) = self._complete_reconcile_case(Path(temporary))
            authority = {
                "enabled": False,
                "attention_phase": "switching",
                "transaction_phase": "starting",
                "failed_sha": request["failed"]["sha"],
                "current_sha": request["prior"]["sha"],
                "installed_controller_files_digest": request["failed"][
                    "installed_controller_files_digest"
                ],
            }
            postgres_format = validated["artifacts"]["platform.json"][
                "postgres_format"
            ]
            postgres = {
                "schema": request["prior"]["schema"],
                "binary_version": postgres_format["candidate_binary_version"],
                "server_version": postgres_format["current_server_version"],
                "data_pg_version": postgres_format["data_pg_version"],
            }
            images = {
                service: evidence["preflight"]["containers"][service]["image_id"]
                for service in ("postgres", "control", "worker", "web")
            }
            self.assertEqual(
                carry.validate_group2_reconcile_preflight(
                    request,
                    {
                        "stage": evidence["preflight"],
                        "authority": authority,
                        "postgres": postgres,
                        "images": images,
                    },
                    {
                        "validated": validated,
                        "authority": authority,
                        "postgres": postgres,
                        "images": images,
                        "kernel": validated["artifacts"]["authority.json"][
                            "vm_inventory"
                        ]["current_kernel"],
                    },
                ),
                evidence["preflight"],
            )
            receipt = carry.validate_group2_reconcile_result(
                request, evidence, validated
            )
            originals = carry._group2_reconcile_originals(validated)
            embedded = {
                name: {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_b64": __import__("base64").b64encode(raw).decode(),
                }
                for name, raw in artifacts.items()
            }
            chain = {
                "schema": "group2-reconcile-chain-v1",
                "request": request,
                "approval": approval,
                "failed_manifest": originals["manifest"],
                "prior_success": request["prior"],
                "first_startup": {
                    "source_artifacts": embedded,
                    **originals["first_startup"],
                },
                "pre_rollback": evidence["preflight"],
                "stopped": evidence["stopped"],
                "restore_startup": evidence["restore_startup"],
                "restored": {
                    "postgres": evidence["restored_postgres"],
                    "final": evidence["restored"],
                    "account": evidence["account"],
                    "images": evidence["images"],
                    "storage": evidence["storage"],
                    "token_health": evidence["token_health"],
                },
                "receipt": receipt,
            }
            inherited = {
                item["name"]: item["sha256"]
                for item in validated["artifacts"]["source-review.json"][
                    "inherited_reviews"
                ]
            }
            with mock.patch.object(
                carry, "GROUP2_INHERITED_REVIEW_HASHES", inherited
            ):
                snapshot = carry.validate_group2_reconcile_preservation(
                    chain, reference
                )
            self.assertEqual(snapshot["db"], originals["db"])
            self.assertEqual(snapshot["files"], evidence["restored"]["files"])

            normal = (
                'x 127.0.0.1:1 - "POST /api/workers/7/heartbeat HTTP/1.1" 204\n'
                'x 127.0.0.1:2 - "POST /api/workers/7/cleanups/claim HTTP/1.1" 204\n'
            )
            _before, normal_logs = Group2RuntimeTests()._log_window(normal)
            carry._validate_reconcile_log_activity(
                normal_logs, allow_registration=False
            )
            _before, business_logs = Group2RuntimeTests()._log_window(
                'x 127.0.0.1:3 - "POST /api/adapters HTTP/1.1" 201\n'
            )
            with self.assertRaisesRegex(
                carry.CarryForwardError, "startup_changed"
            ):
                carry._validate_reconcile_log_activity(
                    business_logs, allow_registration=False
                )

            for label, mutate in (
                (
                    "control db drift",
                    lambda value: value["stopped"]["control"]["db"].__setitem__(
                        "projection", {}
                    ),
                ),
                (
                    "apps files drift",
                    lambda value: value["stopped"]["apps"]["files"]["runtime"][
                        "root"
                    ].__setitem__("inode", 999),
                ),
                (
                    "postgres db drift",
                    lambda value: value["restored_postgres"]["db"].__setitem__(
                        "schema_inventory", {}
                    ),
                ),
            ):
                changed = copy.deepcopy(evidence)
                mutate(changed)
                with self.subTest(label=label), self.assertRaises(
                    carry.CarryForwardError
                ):
                    carry.validate_group2_reconcile_result(
                        request, changed, validated
                    )

            transitions = (
                (
                    "control projection",
                    evidence["preflight"], evidence["stopped"]["control"],
                    "projection", False,
                ),
                (
                    "apps responsibilities",
                    evidence["stopped"]["control"], evidence["stopped"]["apps"],
                    "responsibilities", True,
                ),
                (
                    "postgres inventory",
                    evidence["stopped"]["apps"], evidence["restored_postgres"],
                    "schema_inventory", True,
                ),
            )
            for label, previous, current, key, require_idle in transitions:
                previous = copy.deepcopy(previous)
                current = copy.deepcopy(current)
                current["db"] = copy.deepcopy(current["db"])
                current["db"][key] = {"real_drift": True}
                actions = []
                with self.subTest(label=label), self.assertRaisesRegex(
                    carry.CarryForwardError, "db_changed"
                ):
                    carry.validate_group2_reconcile_transition(
                        previous,
                        current,
                        kernel_baseline=evidence["preflight"]["kernel"],
                        require_idle=require_idle,
                    )
                    actions.append("next_mutation")
                self.assertEqual(actions, [])

            before_logs, after_logs = Group2RuntimeTests()._log_window(
                'x 127.0.0.1:3 - "POST /api/adapters HTTP/1.1" 201\n'
            )
            previous = copy.deepcopy(evidence["preflight"])
            current = copy.deepcopy(evidence["stopped"]["control"])
            previous["logs"] = before_logs
            current["logs"] = after_logs
            previous["window_start_ns"] = 9
            previous["window_end_ns"] = 10
            current["window_start_ns"] = 10
            current["window_end_ns"] = 20
            actions = []
            with self.assertRaisesRegex(
                carry.CarryForwardError, "startup_changed"
            ):
                carry.validate_group2_reconcile_transition(
                    previous,
                    current,
                    kernel_baseline=evidence["preflight"]["kernel"],
                    require_idle=False,
                )
                actions.append("next_mutation")
            self.assertEqual(actions, [])

            changed = copy.deepcopy(evidence)
            for stage in (
                changed["preflight"], changed["stopped"]["control"],
                changed["stopped"]["apps"], changed["restored_postgres"],
                changed["restored"],
            ):
                stage["files"]["runtime"]["root"]["inode"] += 123
            with self.assertRaisesRegex(
                carry.CarryForwardError, "originals_changed|files_changed"
            ):
                carry.validate_group2_reconcile_result(request, changed, validated)

            for label, mutate, code in (
                (
                    "reused nonce",
                    lambda value: value["restore_startup"]["proof"].__setitem__(
                        "nonce", originals["first_startup"]["proof"]["nonce"]
                    ),
                    "startup",
                ),
                (
                    "disconnected log",
                    lambda value: value["restore_startup"]["request"].__setitem__(
                        "logs_before", value["preflight"]["logs"]
                    ),
                    "startup",
                ),
                (
                    "boolean kernel",
                    lambda value: value["stopped"]["apps"].__setitem__(
                        "kernel",
                        {
                            "idle": True,
                            "namespace_quiet": True,
                            "fd_quiet": True,
                            "keeper_unchanged": True,
                        },
                    ),
                    "kernel|stage",
                ),
                (
                    "reused container",
                    lambda value: value["restored"]["containers"]["worker"].__setitem__(
                        "container_id",
                        originals["first_startup"]["proof"]["container_id"],
                    ),
                    "startup|kernel",
                ),
            ):
                changed = copy.deepcopy(evidence)
                mutate(changed)
                with self.subTest(label=label), self.assertRaisesRegex(
                    carry.CarryForwardError, code
                ):
                    carry.validate_group2_reconcile_result(
                        request, changed, validated
                    )

            changed_chain = copy.deepcopy(chain)
            changed_chain["receipt"]["evidence_digest"] = "0" * 64
            with mock.patch.object(
                carry, "GROUP2_INHERITED_REVIEW_HASHES", inherited
            ), self.assertRaisesRegex(carry.CarryForwardError, "chain_changed"):
                carry.validate_group2_reconcile_preservation(
                    changed_chain, reference
                )
            changed = copy.deepcopy(evidence)
            for stage in (
                changed["preflight"], changed["stopped"]["control"],
                changed["stopped"]["apps"], changed["restored_postgres"],
                changed["restored"],
            ):
                stage["kernel"]["keeper_starttime"] = "detached-generation"
            with self.assertRaisesRegex(
                carry.CarryForwardError, "originals_changed"
            ):
                carry.validate_group2_reconcile_result(request, changed, validated)
            for label, mutate in (
                (
                    "worker root direct process",
                    lambda kernel, worker_id: kernel["children"][worker_id].update(
                        process_count=1,
                        process_digest=carry.digest(
                            [kernel["old_worker_authority"]["pid"]]
                        ),
                    ),
                ),
                (
                    "worker agent digest",
                    lambda kernel, worker_id: kernel["children"][
                        f"{worker_id}/agent"
                    ].__setitem__("process_digest", carry.digest([])),
                ),
                (
                    "extra child",
                    lambda kernel, _worker_id: kernel["children"].__setitem__(
                        "unexpected", {
                            "populated": 0, "process_count": 0,
                            "process_digest": carry.digest([]),
                            "device": 1, "inode": 99,
                        }
                    ),
                ),
            ):
                changed = copy.deepcopy(evidence)
                kernel = changed["restored"]["kernel"]
                worker_id = changed["restore_startup"]["proof"]["container_id"]
                mutate(kernel, worker_id)
                with self.subTest(label=label), self.assertRaisesRegex(
                    carry.CarryForwardError, "kernel_changed"
                ):
                    carry.validate_group2_reconcile_result(
                        request, changed, validated
                    )
            changed_chain = copy.deepcopy(chain)
            for stage in (
                changed_chain["pre_rollback"], changed_chain["stopped"]["control"],
                changed_chain["stopped"]["apps"],
                changed_chain["restored"]["postgres"],
                changed_chain["restored"]["final"],
            ):
                stage["kernel"]["keeper_starttime"] = "detached-generation"
            with mock.patch.object(
                carry, "GROUP2_INHERITED_REVIEW_HASHES", inherited
            ), self.assertRaisesRegex(carry.CarryForwardError, "originals_changed"):
                carry.validate_group2_reconcile_preservation(
                    changed_chain, reference
                )
            changed_chain = copy.deepcopy(chain)
            source = changed_chain["first_startup"]["source_artifacts"][
                "failed-manifest.json"
            ]
            source["content_b64"] = __import__("base64").b64encode(b"{}").decode()
            source["sha256"] = hashlib.sha256(b"{}").hexdigest()
            with mock.patch.object(
                carry, "GROUP2_INHERITED_REVIEW_HASHES", inherited
            ), self.assertRaises(carry.CarryForwardError):
                carry.validate_group2_reconcile_preservation(
                    changed_chain, reference
                )

    def test_reconcile_user_record_binds_exact_request_approval_and_actions(self):
        request, approval, _artifacts = self._request()
        record = {
            "schema": "group2-starting-reconcile-user-record-v1",
            "request_digest": request["request_digest"],
            "tool_sha": request["tool"]["sha"],
            "actions": list(carry.GROUP2_RECONCILE_ACTIONS),
            "presented_request": " ".join(
                (
                    request["request_digest"],
                    request["tool"]["sha"],
                    *carry.GROUP2_RECONCILE_ACTIONS,
                )
            ),
            "user_reply": approval["user_reply"],
        }
        raw = carry.canonical_bytes(record)
        approval["user_record_sha256"] = hashlib.sha256(raw).hexdigest()
        self.assertEqual(
            carry.validate_group2_reconcile_user_record(request, approval, raw),
            record,
        )
        for label, mutate in (
            ("wrong raw digest", lambda value: value.__setitem__("user_reply", "changed")),
            ("missing presented action", lambda value: value.__setitem__("presented_request", request["request_digest"])),
            ("extra field", lambda value: value.__setitem__("generated", True)),
        ):
            changed = copy.deepcopy(record)
            mutate(changed)
            with self.subTest(label=label), self.assertRaisesRegex(
                carry.CarryForwardError, "approval_record_invalid"
            ):
                carry.validate_group2_reconcile_user_record(
                    request, approval, carry.canonical_bytes(changed)
                )

    def test_reconcile_wrappers_bind_full_source_review_and_exact_ci(self):
        request, _approval, artifacts = self._request()
        parsed = {name: json.loads(value) for name, value in artifacts.items()}
        raw_hashes = {name: hashlib.sha256(value).hexdigest() for name, value in artifacts.items()}
        first = parsed["first-startup.json"]
        baseline = {"evidence_digest": "1" * 64}
        first["check_raw"].update(
            {
                "after-migration/db.json": {},
                "after-migration/files.json": {},
                "group2/account-check.json": {"account_check": {"containers": {"worker": {}}}},
                "group2/log-before-start.json": {"log_evidence": baseline},
                "group2/log-after-start-request.json": {"baseline": baseline},
                "group2/start-window.json": {"window_start_ns": 1, "window_end_ns": 2},
            }
        )
        first["log_read_diagnostic"] = {"log_append": {}}
        first["db"] = {
            "asset_projection": {
                name: {"columns": [], "primary_key": [], "rows": [], "count": 0}
                for name in carry.ASSET_TABLES
            }
        }
        first["legacy_assets"] = {
            name: {"columns": [], "rows": []} for name in carry.ASSET_TABLES
        }
        proof, startup = {"proof": True}, {"code": "ok"}
        first["actual_results"] = {
            "startup_reconstruction": {
                "proof": proof,
                "result": "DERIVED_FROM_LATER_DIAGNOSTIC_ONLY",
            }
        }
        raw_hashes["first-startup.json"] = hashlib.sha256(
            carry.canonical_bytes(first)
        ).hexdigest()
        request["evidence_files"]["first-startup.json"] = raw_hashes[
            "first-startup.json"
        ]
        request["failed"]["initial_evidence_digest"] = carry.digest(
            {
                name: raw_hashes[name]
                for name in (
                    "failed-manifest.json",
                    "first-startup.json",
                    "prior-success.json",
                    "authority.json",
                    "platform.json",
                )
            }
        )
        with (
            mock.patch.object(carry, "_startup_proof", return_value=proof),
            mock.patch.object(carry, "compare_group2_startup_files", return_value=startup),
            mock.patch.object(carry, "_validate_group2_manifest_db"),
            mock.patch.object(
                carry,
                "GROUP2_INHERITED_REVIEW_HASHES",
                {
                    item["name"]: item["sha256"]
                    for item in parsed["source-review.json"]["inherited_reviews"]
                },
            ),
        ):
            carry._validate_reconcile_wrappers(request, parsed, raw_hashes)
            changed = copy.deepcopy(parsed)
            changed["source-review.json"]["tool_review"]["entries"].pop()
            with self.assertRaisesRegex(carry.CarryForwardError, "review_invalid"):
                carry._validate_reconcile_wrappers(request, changed, raw_hashes)
            changed = copy.deepcopy(parsed)
            changed["ci.json"]["raw"]["jobs"]["jobs"][0]["conclusion"] = "failure"
            with self.assertRaisesRegex(carry.CarryForwardError, "ci_invalid"):
                carry._validate_reconcile_wrappers(request, changed, raw_hashes)

    def test_vm_reconcile_orders_restore_without_migrate_restore_or_retag(self):
        request, approval, artifacts = self._request()
        manifest = json.loads(artifacts["failed-manifest.json"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incident = root / "incidents" / request["incident_id"]
            evidence = incident / "evidence"
            tool = incident / "tool"
            evidence.mkdir(parents=True, mode=0o700)
            tool.mkdir(mode=0o700)
            (root / "incidents").chmod(0o700)
            incident.chmod(0o700)
            for name, value in artifacts.items():
                (evidence / name).write_bytes(value)
            carry.write_private(incident / "request.json", request)
            carry.write_private(incident / "approval.json", approval)
            carry.write_private(
                incident / "preflight-authority.json",
                {
                    "enabled": False,
                    "attention_phase": "switching",
                    "transaction_phase": "starting",
                    "failed_sha": request["failed"]["sha"],
                    "current_sha": request["prior"]["sha"],
                    "installed_controller_files_digest": request["failed"]["installed_controller_files_digest"],
                },
            )
            (root / "current-sha").write_text(request["prior"]["sha"])
            carry.write_private(
                root / "transaction.json",
                {"phase": "starting", "sha": request["failed"]["sha"], "backup": "old", "carry_forward": {"manifest_id": request["failed"]["manifest_id"]}},
            )
            carry.write_private(
                root / "deployment.json",
                {"sandbox_unit": "unit", "sandbox_cpu_quota": "100%", "sandbox_memory_max": "1G"},
            )
            installed = {}
            for name in ("deploy.sh", "carry_forward.py", "verify.py", "assets.py"):
                (root / name).write_bytes(name.encode())
                installed[name] = hashlib.sha256(name.encode()).hexdigest()
            manifest["kernel_evidence"] = {"old_worker_authority": {}}
            manifest["file_evidence"].setdefault("journal_facts", {})["sandbox_recovery"] = []
            project = manifest["account_entry"]["project"]
            manifest["old_image_ids"] = {
                f"{project}-{service}:{carry.GROUP2_FROM_SHA}": f"sha256:{index:064x}"
                for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
            }
            manifest["storage_identity"] = [
                {"service": "worker", "type": "volume", "source": "runtime", "destination": "/var/lib/dlr/runtime", "read_only": False},
                {"service": "worker", "type": "volume", "source": "journal", "destination": "/var/lib/dlr/journal", "read_only": False},
            ]
            request["restore"]["storage_identity_digest"] = carry.digest(
                manifest["storage_identity"]
            )
            carry.write_private(incident / "request.json", request)
            old_images = {}
            for service in ("postgres", "control", "worker", "web"):
                matches = [
                    image for tag, image in manifest["old_image_ids"].items()
                    if tag.endswith(f"-{service}:{carry.GROUP2_FROM_SHA}")
                ]
                old_images[service] = matches[0]
            parsed = {name: json.loads(value) for name, value in artifacts.items()}
            parsed["failed-manifest.json"] = manifest
            parsed["first-startup.json"] = {
                "db": {},
                "files": {},
                "log_read_diagnostic": {"log_append": {}},
                "actual_results": {"startup_reconstruction": {"proof": {}}},
            }
            parsed["platform.json"]["images"] = {
                "candidate": {service: {"Id": image} for service, image in old_images.items()},
                "prior": {service: {"Id": image} for service, image in old_images.items()},
            }
            user_record = carry.canonical_bytes(
                {
                    "schema": "group2-starting-reconcile-user-record-v1",
                    "request_digest": request["request_digest"],
                    "tool_sha": request["tool"]["sha"],
                    "actions": list(carry.GROUP2_RECONCILE_ACTIONS),
                    "presented_request": " ".join(
                        (
                            request["request_digest"],
                            request["tool"]["sha"],
                            *carry.GROUP2_RECONCILE_ACTIONS,
                        )
                    ),
                    "user_reply": approval["user_reply"],
                }
            )
            approval["user_record_sha256"] = hashlib.sha256(user_record).hexdigest()
            carry.write_private(incident / "approval.json", approval)
            (incident / "USER-APPROVAL.txt").write_bytes(user_record)
            carry.write_private(
                incident / "phase.json",
                {
                    "schema": "group2-starting-reconcile-phase-v1",
                    "incident_id": request["incident_id"],
                    "phase": "prepared",
                },
            )
            for name in ("deploy.sh", "carry_forward.py"):
                payload = name.encode()
                (tool / name).write_bytes(payload)
                request["tool"]["controller_files"][name] = hashlib.sha256(
                    payload
                ).hexdigest()
            carry.write_private(incident / "request.json", request)
            commands = []
            real_atomic = carry._atomic_incident_json

            def atomic(path, value):
                real_atomic(path, value)
                if path.name == "result.json":
                    carry.write_private(
                        incident / "host-validated.json",
                        {"incident_id": request["incident_id"], "receipt_digest": "9" * 64},
                    )

            def run(arguments, **_kwargs):
                commands.append(arguments)
                return SimpleNamespace(returncode=0)

            def checked(arguments):
                if any("SELECT version_num" in item for item in arguments):
                    return request["prior"]["schema"]
                if "--version" in arguments:
                    return "postgres (PostgreSQL) 16.15"
                if "SHOW server_version" in arguments:
                    return "16.15"
                if any("PG_VERSION" in item for item in arguments):
                    return "16"
                return ""

            receipt = {"receipt_digest": "9" * 64}
            with (
                mock.patch.object(carry, "validate_group2_reconcile_request", return_value={"request": request, "approval": approval, "artifacts": parsed}),
                mock.patch.object(carry, "_live_storage_identity", return_value=manifest["storage_identity"]),
                mock.patch.object(carry, "read_log_append", return_value={}),
                mock.patch.object(carry, "_capture_reconcile_state", return_value=({}, {})),
                mock.patch.object(
                    carry,
                    "capture_kernel",
                    autospec=True,
                    return_value={"old_worker_authority": {}},
                ),
                mock.patch.object(carry, "_container_image", side_effect=lambda _p, service: old_images.get(service, "rabbit")),
                mock.patch.object(carry, "_inspect_container", return_value={"container_id": "same", "status": "exited"}),
                mock.patch.object(carry, "validate_group2_reconcile_preflight", side_effect=lambda _request, fresh, _originals: fresh["stage"]),
                mock.patch.object(carry, "validate_group2_reconcile_transition"),
                mock.patch.object(carry, "_validate_reconcile_container_transition"),
                mock.patch.object(carry, "_incident_startup_proof", return_value={}),
                mock.patch.object(carry, "_read_token_health", return_value={"status": 200, "database": True}),
                mock.patch.object(carry, "validate_group2_reconcile_result", return_value=receipt),
                mock.patch.object(carry, "_checked_output", side_effect=checked),
                mock.patch.object(carry.subprocess, "run", side_effect=run),
                mock.patch.object(carry, "_atomic_incident_json", side_effect=atomic),
            ):
                result = carry.reconcile_group2_vm(root, request["incident_id"])
            self.assertEqual(result["receipt_digest"], "9" * 64)
            flattened = "\n".join(" ".join(command) for command in commands)
            self.assertIn("stop control", flattened)
            self.assertIn("--force-recreate --wait", flattened)
            self.assertNotIn("pg_restore", flattened)
            self.assertNotIn("alembic", flattened)
            self.assertNotIn("docker tag", flattened)
            committed = carry.read_private(root / "transaction.json")
            self.assertEqual(committed["operation"], "incident_software_restore")
            self.assertEqual(committed["backup"], "/private/backup")
            self.assertEqual(committed["carry_forward"], parsed["authority.json"]["snapshot"]["state"]["carry_forward"])

            for fail_at, forbidden in (
                (1, ("stop worker web account-web", "postgres", "control worker web")),
                (2, ("postgres", "control worker web")),
                (3, ("control worker web",)),
            ):
                carry.write_private(
                    incident / "phase.json",
                    {
                        "schema": "group2-starting-reconcile-phase-v1",
                        "incident_id": request["incident_id"],
                        "phase": "prepared",
                    },
                )
                carry.write_private(
                    root / "transaction.json",
                    {"phase": "starting", "sha": request["failed"]["sha"]},
                )
                commands.clear()
                calls = 0

                def reject_stage(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise carry.CarryForwardError("group2_reconcile_db_changed")

                with (
                    mock.patch.object(carry, "validate_group2_reconcile_request", return_value={"request": request, "approval": approval, "artifacts": parsed}),
                    mock.patch.object(carry, "_live_storage_identity", return_value=manifest["storage_identity"]),
                    mock.patch.object(carry, "read_log_append", return_value={}),
                    mock.patch.object(carry, "_capture_reconcile_state", return_value=({}, {})),
                    mock.patch.object(carry, "capture_kernel", autospec=True, return_value={"old_worker_authority": {}}),
                    mock.patch.object(carry, "_container_image", side_effect=lambda _p, service: old_images.get(service, "rabbit")),
                    mock.patch.object(carry, "_inspect_container", return_value={"container_id": "same", "status": "exited"}),
                    mock.patch.object(carry, "validate_group2_reconcile_preflight", side_effect=lambda _request, fresh, _originals: fresh["stage"]),
                    mock.patch.object(carry, "validate_group2_reconcile_transition", side_effect=reject_stage),
                    mock.patch.object(carry, "_validate_reconcile_container_transition"),
                    mock.patch.object(carry, "_checked_output", side_effect=checked),
                    mock.patch.object(carry.subprocess, "run", side_effect=run),
                    self.assertRaisesRegex(carry.CarryForwardError, "db_changed"),
                ):
                    carry.reconcile_group2_vm(root, request["incident_id"])
                flattened = "\n".join(" ".join(command) for command in commands)
                for fragment in forbidden:
                    with self.subTest(fail_at=fail_at, fragment=fragment):
                        self.assertNotIn(fragment, flattened)

    def test_partial_finalize_vm_orchestration_rejects_replay_after_ack_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incident_id = carry.GROUP2_PARTIAL_INCIDENT_ID
            finalize_id = "f" * 32
            directory = root / "incidents" / incident_id / "finalize" / finalize_id
            evidence_dir = directory / "evidence"
            tool_dir = directory / "tool"
            tool_dir.mkdir(parents=True, mode=0o700)
            evidence_dir.mkdir(mode=0o700)
            for parent in (root / "incidents", root / "incidents" / incident_id,
                           root / "incidents" / incident_id / "finalize", directory):
                parent.chmod(0o700)
            tool_hashes = {}
            for name in ("deploy.sh", "carry_forward.py"):
                raw = ("tool-" + name).encode()
                (tool_dir / name).write_bytes(raw)
                tool_hashes[name] = hashlib.sha256(raw).hexdigest()
            for name in carry.GROUP2_PARTIAL_FINALIZE_FILES:
                (evidence_dir / name).write_text("{}")
            request = {
                "incident_id": incident_id, "finalize_id": finalize_id,
                "request_digest": "b" * 64,
                "tool": {"controller_files": tool_hashes},
            }
            approval = {"status": "USER_APPROVED"}
            carry.write_private(directory / "request.json", request)
            carry.write_private(directory / "approval.json", approval)
            (directory / "USER-APPROVAL.txt").write_text("{}")
            (directory / "USER-APPROVAL.txt").chmod(0o600)
            carry.write_private(directory / "phase.json", {
                "schema": "group2-partial-finalize-phase-v1",
                "incident_id": incident_id, "finalize_id": finalize_id,
                "phase": "prepared",
            })
            carry.write_private(root / "deployment.json", {})
            original_phase = {
                "schema": "group2-starting-reconcile-phase-v1",
                "incident_id": incident_id, "phase": "restoring-apps",
            }
            carry.write_private(root / "incidents" / incident_id / "phase.json",
                                original_phase)
            carry.write_private(root / "transaction.json", {
                "phase": "starting", "sha": carry.GROUP2_PARTIAL_FAILED_SHA,
            })
            (root / "current-sha").write_text(carry.GROUP2_FROM_SHA + "\n")
            installed = {}
            for name in ("deploy.sh", "carry_forward.py", "verify.py", "assets.py"):
                raw = ("installed-" + name).encode()
                (root / name).write_bytes(raw)
                installed[name] = hashlib.sha256(raw).hexdigest()
            containers = {
                name: {"container_id": name, "status": "running"}
                for name in ("postgres", "rabbitmq", "control", "worker", "web", "account-web")
            }
            current = {"containers": containers}
            manifest = {
                "account_entry": {"project": "example"},
                "storage_identity": [
                    {"service": "worker", "type": "volume", "source": "runtime",
                     "destination": "/var/lib/dlr/runtime"},
                    {"service": "worker", "type": "volume", "source": "journal",
                     "destination": "/var/lib/dlr/journal"},
                ],
                "kernel_evidence": {"old_worker_authority": {}},
                "review_scope": {"preservation_reference": {"snapshot": {}}},
            }
            prior_state = {"backup": "/old/backup", "carry_forward": {"manifest_id": "a" * 32}}
            validated = {
                "request": request, "approval": approval, "user_record": {},
                "artifacts": {},
                "context": {
                    "originals": {"manifest": manifest},
                    "diagnostic": {"stage": {"logs": {}}},
                    "validated_original": {"artifacts": {
                        "platform.json": {"postgres_format": {
                            "prior_binary_version": "postgres 16.15",
                            "current_server_version": "16.15",
                            "data_pg_version": "16",
                        }},
                        "prior-success.json": {"host": {}, "vm": {}},
                        "authority.json": {"snapshot": {"state": prior_state}},
                    }},
                },
            }
            host_authority = {
                "host_files": {}, "host_installed": {},
                "prior_success": {"host": {}, "vm": {}},
            }
            carry.write_private(directory / "host-authority.json", host_authority)
            receipt = {"receipt_digest": "9" * 64}
            carry.write_private(directory / "host-validated.json", {
                "finalize_id": finalize_id,
                "request_digest": request["request_digest"],
                "result_digest": "8" * 64,
                "receipt_digest": receipt["receipt_digest"],
                "chain_digest": "7" * 64,
            })
            capture_calls = []

            def capture(*args, **kwargs):
                capture_calls.append((args, kwargs))
                return current

            def checked(arguments):
                text = " ".join(arguments)
                if "version_num" in text:
                    return "0040_issue152_dispositions"
                if "postgres --version" in text:
                    return "postgres 16.15"
                if "server_version" in text:
                    return "16.15"
                return "16"

            with (
                mock.patch.object(carry, "validate_group2_partial_finalize_request", return_value=validated),
                mock.patch.object(carry, "_capture_reconcile_stage", side_effect=capture),
                mock.patch.object(carry, "_checked_output", side_effect=checked),
                mock.patch.object(carry, "_read_token_health", return_value={"status": 200, "database": True}),
                mock.patch.object(carry, "validate_group2_partial_finalize_result", return_value=receipt),
                mock.patch.object(carry, "_validate_group2_partial_finalize_preservation", return_value={}),
                mock.patch.object(carry, "streaming_digest", return_value="7" * 64),
                mock.patch.object(carry, "digest", return_value="8" * 64),
                mock.patch.object(carry, "_inspect_container", side_effect=lambda _project, service: containers[service]),
            ):
                sibling = directory.parent / ("e" * 32)
                sibling.mkdir(mode=0o700)
                (sibling / "marker").write_text("existing")
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "replay_rejected"
                ):
                    carry.finalize_group2_partial_vm(
                        root, incident_id, finalize_id
                    )
                self.assertEqual(capture_calls, [])
                (sibling / "marker").unlink()
                sibling.rmdir()

                real_identity = carry._validate_group2_partial_finalize_directory
                identity_calls = 0

                def add_sibling_before_commit(*args):
                    nonlocal identity_calls
                    identity_calls += 1
                    if identity_calls == 2:
                        sibling.mkdir(mode=0o700)
                        (sibling / "marker").write_text("late")
                    return real_identity(*args)

                with (
                    mock.patch.object(
                        carry,
                        "_validate_group2_partial_finalize_directory",
                        side_effect=add_sibling_before_commit,
                    ),
                    self.assertRaisesRegex(
                        carry.CarryForwardError, "replay_rejected"
                    ),
                ):
                    carry.finalize_group2_partial_vm(
                        root, incident_id, finalize_id
                    )
                self.assertEqual(len(capture_calls), 1)
                self.assertEqual(
                    carry.read_private(root / "transaction.json")["phase"],
                    "starting",
                )
                (sibling / "marker").unlink()
                sibling.rmdir()
                carry.write_private(directory / "phase.json", {
                    "schema": "group2-partial-finalize-phase-v1",
                    "incident_id": incident_id, "finalize_id": finalize_id,
                    "phase": "prepared",
                })
                output = carry.finalize_group2_partial_vm(
                    root, incident_id, finalize_id
                )
                self.assertEqual(output["receipt_digest"], receipt["receipt_digest"])
                self.assertEqual(carry.read_private(root / "transaction.json")["operation"],
                                 "incident_partial_finalize")
                self.assertEqual(len(capture_calls), 2)
                with self.assertRaisesRegex(
                    carry.CarryForwardError, "replay_rejected"
                ):
                    carry.finalize_group2_partial_vm(root, incident_id, finalize_id)
                self.assertEqual(len(capture_calls), 2)


if __name__ == "__main__":
    unittest.main()
