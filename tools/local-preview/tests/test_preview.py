import sys
import tempfile
import unittest
import json
import os
import hashlib
import copy
import http.server
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preview
import carry_forward
from migrations import compatible, graph

A, B = "a" * 40, "b" * 40


class MigrationTests(unittest.TestCase):
    def test_forward_and_immutable_history(self):
        old = {"1.py": "revision = '1'\ndown_revision = None"}
        new = {**old, "2.py": "revision: str = '2'\ndown_revision = '1'"}
        self.assertEqual(compatible(old, new, "1"), "2")
        self.assertEqual(
            compatible(
                old, {**new, "1.py": old["1.py"] + "\n# reviewed historical fix"}, "1"
            ),
            "2",
        )
        for bad in (
            {"2.py": new["2.py"]},
            {**new, "1.py": "revision='1'\ndown_revision='2'"},
        ):
            with self.assertRaises(ValueError):
                compatible(old, bad, "1")
        with self.assertRaises(ValueError):
            compatible(old, new, "unrelated")

    def test_unexecuted_code_and_broken_graphs(self):
        self.assertEqual(
            graph({"1.py": "raise RuntimeError()\nrevision='1'\ndown_revision=None"})[
                0
            ],
            "1",
        )
        for source in (
            "revision='1'\ndown_revision='1'",
            "revision='1'\ndown_revision='missing'",
            "revision='1'\ndown_revision=('a','b')",
            "revision=make_id()\ndown_revision=None",
            "revision='1'\ndown_revision=None\ndepends_on='external'",
        ):
            with self.assertRaises((ValueError, KeyError)):
                graph({"1.py": source})


class AuditedSourceDiffTests(unittest.TestCase):
    def git_result(self, *args):
        if args[:2] == ("cat-file", "-e"):
            return ""
        if args[:2] == ("cat-file", "-t"):
            return "blob\n"
        if args[0] == "rev-parse":
            return ("1" if args[1].startswith(A) else "2") * 40 + "\n"
        raise AssertionError(args)

    def test_raw_git_diff_is_closed_and_tree_bound(self):
        old_oid, new_oid = "3" * 40, "4" * 40
        raw = (
            f":100644 100644 {old_oid} {new_oid} M".encode()
            + b"\0web/src/index.css\0"
            + f":100755 100755 {'5' * 40} {'6' * 40} M".encode()
            + b"\0tools/local-preview/preview.py\0"
            + f":100755 100755 {'7' * 40} {'8' * 40} M".encode()
            + b"\0tools/local-preview/deploy.sh\0"
        )
        with (
            patch.object(preview, "git", side_effect=self.git_result),
            patch.object(preview, "git_bytes", return_value=raw),
        ):
            result = preview.audited_source_diff(A, B)
        by_path = {item["path"]: item for item in result["entries"]}
        self.assertIn("web/src/index.css", by_path)
        self.assertEqual(
            by_path["tools/local-preview/preview.py"]["old_mode"], "100755"
        )
        self.assertEqual(by_path["tools/local-preview/deploy.sh"]["new_mode"], "100755")
        self.assertEqual(
            result["tree_digest"],
            carry_forward.digest(
                {
                    "from_tree": "1" * 40,
                    "to_tree": "2" * 40,
                    "entries": result["entries"],
                }
            ),
        )

    def test_unknown_deleted_renamed_or_nonregular_source_is_rejected(self):
        cases = (
            ("M", "100644", "100644", "backend/src/unknown.py"),
            ("D", "100644", "000000", "web/src/index.css"),
            ("R", "100644", "100644", "web/src/index.css"),
            ("M", "120000", "120000", "web/src/index.css"),
            ("M", "160000", "160000", "web/src/index.css"),
            ("M", "100755", "100755", "web/src/index.css"),
            ("M", "100644", "100644", "tools/local-preview/preview.py"),
        )
        for status, old_mode, new_mode, path in cases:
            raw = (
                f":{old_mode} {new_mode} {'3' * 40} {'4' * 40} {status}".encode()
                + b"\0"
                + path.encode()
                + b"\0"
            )
            with self.subTest(status=status, mode=old_mode, path=path):
                with (
                    patch.object(preview, "git", side_effect=self.git_result),
                    patch.object(preview, "git_bytes", return_value=raw),
                ):
                    with self.assertRaises(ValueError):
                        preview.audited_source_diff(A, B)

        raw = (
            f":100644 100644 {'3' * 40} {'4' * 40} M".encode()
            + b"\0docs/en/local-preview.md\0"
        )
        with (
            patch.object(preview, "git", side_effect=self.git_result),
            patch.object(preview, "git_bytes", return_value=raw),
        ):
            with self.assertRaisesRegex(ValueError, "missing the approved Web fix"):
                preview.audited_source_diff(A, B)

    def test_switch_recomputes_source_diff_before_phase(self):
        manifest_id = "1" * 32
        manifest_digest = "2" * 64
        reference = {
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
            "from_sha": A,
            "to_sha": B,
            "from_schema": "0040_issue152_dispositions",
            "to_schema": "0040_issue152_dispositions",
            "pr": 2,
        }
        manifest = {
            **reference,
            "repo": "owner/repo",
            "mode": carry_forward.AUDITED_MODE,
            "controller_files_digest": "3" * 64,
            "migration_graph_digest": "4" * 64,
            "source_diff": {"tree_digest": "5" * 64, "entries": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(preview, "ROOT", root),
                patch.object(carry_forward, "read_private", return_value=manifest),
                patch.object(carry_forward, "validate_manifest", return_value=manifest),
                patch.object(preview, "controller_files_digest", return_value="3" * 64),
                patch.object(preview, "migration_graph_digest", return_value="4" * 64),
                patch.object(
                    preview,
                    "audited_source_diff",
                    return_value={"tree_digest": "6" * 64, "entries": []},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "source difference changed"):
                    preview.selected_manifest(
                        {"repo": "owner/repo", "carry_forward": reference},
                        {"sha": A, "schema": "0040_issue152_dispositions"},
                        {"sha": B, "schema": "0040_issue152_dispositions", "pr": 2},
                    )


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(preview, "ROOT", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = {"repo": "owner/repo", "pr": 2, "enabled": True}
        self.previous = {"sha": A, "pr": 1}
        self.target = {"sha": B, "pr": 2, "run_id": 10, "run_attempt": 1}
        preview.write("config.json", self.config)
        preview.write("state.json", self.previous)
        self.mocks = {}
        for name, value in [
            ("healthy", True),
            ("ensure_vm", None),
            ("stage_source", None),
            ("check_compatibility", None),
            ("phase", True),
            ("transaction", {"phase": "ready", "sha": A}),
            ("receipt", {"probe": {"status": "succeeded"}}),
            ("eligible", (self.target, "CI passed")),
        ]:
            p = patch.object(preview, name, return_value=value)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)

    def test_cross_pr_success(self):
        self.assertEqual(preview.tick(), "Ready")
        self.assertEqual(preview.read("state.json")["sha"], B)
        self.assertEqual(
            [c.args[1] for c in self.mocks["phase"].call_args_list], ["stage", "deploy"]
        )
        self.assertIsNone(preview.read("attention.json"))

    def test_explicit_manifest_is_transferred_and_bound_to_deploy_only(self):
        manifest_path = Path(self.temp.name) / "manifest.json"
        manifest_path.write_text("{}")
        manifest = {"manifest_id": "1" * 32, "manifest_digest": "2" * 64}
        with (
            patch.object(
                preview, "selected_manifest", return_value=(manifest, manifest_path)
            ),
            patch.object(preview, "vm_private_write") as transfer,
        ):
            self.assertEqual(preview.tick(), "Ready")
        self.assertEqual(
            self.mocks["phase"].call_args_list[-1].args,
            (self.target, "deploy", "1" * 32),
        )
        transfer.assert_called_once()
        self.assertFalse(
            (
                Path(self.temp.name) / "carry-forward/manifests/" / manifest_path.name
            ).exists()
        )
        self.assertTrue(
            (Path(self.temp.name) / "carry-forward/consumed/manifest.json").exists()
        )

    def test_ci_rerun_same_sha_does_not_build(self):
        self.target["sha"] = A
        self.assertEqual(preview.tick(), "Ready")
        self.mocks["phase"].assert_not_called()

    def test_target_changed_during_build(self):
        def phase(*_):
            preview.write("config.json", dict(self.config, pr=3))
            return True

        self.mocks["phase"].side_effect = phase
        self.assertIn("Selection changed", preview.tick())
        self.assertEqual(self.mocks["phase"].call_count, 1)
        self.assertEqual(preview.read("state.json"), self.previous)

    def test_latest_head_and_ci_rechecked(self):
        for target in (None, dict(self.target, sha="c" * 40)):
            self.mocks["eligible"].side_effect = [
                (self.target, "CI passed"),
                (target, "changed"),
            ]
            self.assertIn("superseded", preview.tick())
            self.assertEqual(preview.read("state.json"), self.previous)

    def test_build_failure_does_not_pause_or_mutate_deployment(self):
        self.mocks["phase"].side_effect = RuntimeError("build failure")
        with self.assertRaises(RuntimeError):
            preview.tick()
        self.assertIsNone(preview.read("attention.json"))
        self.assertEqual(preview.read("state.json"), self.previous)

    def test_busy_retries_without_attention(self):
        self.mocks["phase"].side_effect = [True, False]
        self.assertIn("idle", preview.tick())
        self.assertIsNone(preview.read("attention.json"))
        self.assertEqual(preview.read("state.json"), self.previous)

    def test_migration_failure_stays_blocked_even_if_old_health_passes(self):
        self.mocks["phase"].side_effect = [True, RuntimeError("migration failed")]
        with self.assertRaises(RuntimeError):
            preview.tick()
        self.assertIsNotNone(preview.read("attention.json"))
        self.assertIn("Needs attention", preview.tick())
        self.assertEqual(preview.read("state.json"), self.previous)

    def test_unfinished_vm_transaction_blocks_switch(self):
        self.mocks["transaction"].return_value = {"phase": "migrating", "sha": B}
        self.assertIn("Unfinished", preview.tick())
        self.assertIsNotNone(preview.read("attention.json"))

    def test_offline_recovery_uses_previous_without_github_or_build(self):
        self.mocks["healthy"].return_value = False
        with patch.object(preview, "vm_running", return_value=False):
            self.assertIn("Recovered", preview.tick())
        self.mocks["phase"].assert_called_once_with(self.previous, "recover")
        self.mocks["eligible"].assert_not_called()
        self.mocks["stage_source"].assert_not_called()

    def test_offline_interrupted_transaction_cannot_recover_old_database(self):
        self.mocks["healthy"].return_value = False
        self.mocks["transaction"].return_value = {"phase": "migrating", "sha": B}
        with patch.object(preview, "vm_running", return_value=False):
            self.assertIn("Unfinished", preview.tick())
        self.mocks["phase"].assert_not_called()
        self.assertIsNotNone(preview.read("attention.json"))

    def test_group2_failed_recovery_cannot_be_washed_to_ready(self):
        previous = {
            **self.previous,
            "mode": carry_forward.GROUP2_MODE,
            "carry_forward": {"manifest_id": "1" * 32},
        }
        preview.write("state.json", previous)
        self.mocks["healthy"].return_value = False
        self.mocks["phase"].side_effect = RuntimeError("post recovery gate failed")
        manifest = {"manifest_id": "1" * 32}
        with (
            patch.object(preview, "vm_running", return_value=False),
            patch.object(preview, "validate_group2_recovery", return_value=manifest),
        ):
            with self.assertRaisesRegex(RuntimeError, "post recovery gate failed"):
                preview.tick()
            marker = preview.read("attention.json")
            self.assertEqual(marker["phase"], "recovering")
            self.mocks["healthy"].return_value = True
            self.assertIn("Needs attention", preview.tick())
        self.assertEqual(preview.read("state.json"), previous)

    def test_paused(self):
        preview.write("config.json", dict(self.config, enabled=False))
        self.assertIn("Paused", preview.tick())
        self.mocks["eligible"].assert_not_called()


class HistoryReconciliationTests(unittest.TestCase):
    def test_private_anchor_requires_identical_runtime_and_forward_history(self):
        previous = {"sha": A, "history_anchor_sha": B, "schema": "1"}
        target = {"sha": "c" * 40}
        with (
            patch.object(preview, "git", side_effect=["", B]),
            patch.object(preview, "container", return_value="example-db"),
            patch.object(
                preview, "vm_command", return_value=SimpleNamespace(stdout="1")
            ),
            patch.object(
                preview,
                "migration_files",
                return_value={"1.py": "revision='1'\ndown_revision=None"},
            ),
        ):
            preview.check_compatibility(previous, target)
            self.assertEqual(target["schema"], "1")
        with (
            patch.object(preview, "git", side_effect=RuntimeError("runtime differs")),
            self.assertRaises(RuntimeError),
        ):
            preview.check_compatibility(previous, target)
        with (
            patch.object(preview, "git", side_effect=["", A]),
            self.assertRaises(ValueError),
        ):
            preview.check_compatibility(previous, target)


class PrivateConfigurationTests(unittest.TestCase):
    def test_missing_and_unsafe_deployment_settings_fail_closed(self):
        config = {
            "profile": "example-vm",
            "project": "example-app",
            "vm_root": "/example/preview",
            "web_port": 12345,
            "launchagent_label": "org.example.preview",
            "sandbox_unit": "example-preview.service",
            "sandbox_cpu_quota": "100%",
            "sandbox_memory_max": "1G",
        }
        with patch.object(preview, "read", return_value=config):
            self.assertEqual(preview.container("worker"), "example-app-worker-1")
            self.assertEqual(
                preview.vm_path("state.json"), "/example/preview/state.json"
            )
        for bad in (
            {},
            dict(config, web_port=True),
            dict(config, project="app;bad"),
            dict(config, vm_root="/example/../other"),
        ):
            with (
                patch.object(preview, "read", return_value=bad),
                self.assertRaises(ValueError),
            ):
                preview.settings()


class RemotePhaseTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(
            preview, "settings", return_value={"vm_root": "/example/preview"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_colima_collapsed_exit_and_stale_receipt(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(preview, "ROOT", Path(directory)),
            patch.object(
                preview.uuid, "uuid4", return_value=SimpleNamespace(hex="nonce")
            ),
        ):
            for record, expected in (("nonce 75", False), ("nonce 0", True)):
                with patch.object(
                    preview,
                    "vm_command",
                    side_effect=[
                        SimpleNamespace(returncode=0 if expected else 1),
                        SimpleNamespace(stdout=record),
                    ],
                ):
                    self.assertIs(preview.phase({"sha": B}, "deploy"), expected)
            for record in ("old 75", "nonce 1", ""):
                with (
                    patch.object(
                        preview,
                        "vm_command",
                        side_effect=[
                            SimpleNamespace(returncode=1),
                            SimpleNamespace(stdout=record),
                        ],
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    preview.phase({"sha": B}, "deploy")

    def test_manifest_is_a_separate_argument_before_nonce(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(preview, "ROOT", Path(directory)),
            patch.object(
                preview.uuid, "uuid4", return_value=SimpleNamespace(hex="nonce")
            ),
            patch.object(
                preview,
                "vm_command",
                side_effect=[
                    SimpleNamespace(returncode=0),
                    SimpleNamespace(stdout="nonce 0"),
                ],
            ) as command,
        ):
            self.assertTrue(preview.phase({"sha": B}, "deploy", "1" * 32))
        remote = command.call_args_list[0].args
        self.assertEqual(
            remote[-4:], ("", "1" * 32, "nonce", "/example/preview/phase-exit")
        )


class CarryForwardSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        patcher = patch.object(preview, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        preview.write("state.json", {"sha": A, "schema": "0038_issue138_languages"})

    def manifest(self, directory):
        path = Path(directory) / "manifest.json"
        Path(directory).chmod(0o700)
        value = carry_forward.seal_manifest(
            {
                "format_version": carry_forward.FORMAT_VERSION,
                "manifest_id": "1" * 32,
                "created_at": "2026-09-17T00:00:00+00:00",
                "repo": "owner/repo",
                "pr": 2,
                "from_sha": A,
                "to_sha": B,
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
                "old_runtime_projection": {
                    name: {
                        "columns": ["id"],
                        "primary_key": ["id"],
                        "rows": [],
                        "count": 0,
                    }
                    for name in carry_forward.RESPONSIBILITY_TABLES
                },
                "schema_inventory": {
                    "tables": sorted(carry_forward.RESPONSIBILITY_TABLES)
                },
                "storage_identity": [],
                "old_containers": [],
                "file_evidence": {},
                "kernel_evidence": {},
            }
        )
        carry_forward.write_private(path, value)
        return path, value

    def test_install_manifest_copies_once_and_keeps_only_safe_reference(self):
        with tempfile.TemporaryDirectory() as source:
            path, manifest = self.manifest(source)
            with (
                patch.object(preview, "controller_files_digest", return_value="c" * 64),
                patch.object(preview, "migration_graph_digest", return_value="d" * 64),
                patch.object(preview, "api", return_value={"head": {"sha": B}}),
            ):
                reference = preview.install_manifest({"repo": "owner/repo"}, 2, path)
        copied = self.root / "carry-forward/manifests" / ("1" * 32 + ".json")
        self.assertTrue(copied.is_file())
        self.assertNotIn("selection", reference)
        self.assertEqual(reference["manifest_digest"], manifest["manifest_digest"])

    def test_plain_select_clears_stale_manifest_reference(self):
        preview.write(
            "config.json",
            {
                "repo": "owner/repo",
                "pr": 1,
                "enabled": False,
                "carry_forward": {"manifest_id": "stale"},
            },
        )
        pull = {
            "state": "open",
            "draft": False,
            "head": {"repo": {"full_name": "owner/repo"}, "sha": B},
        }
        with (
            patch.dict(os.environ, {"DLR_PREVIEW_HOME": str(self.root)}),
            patch.object(sys, "argv", ["preview.py", "select", "2"]),
            patch.object(preview, "api", return_value=pull),
            patch("builtins.print"),
        ):
            preview.main()
        config = preview.read("config.json")
        self.assertNotIn("carry_forward", config)
        self.assertEqual(config["pr"], 2)


class EligibilityTests(unittest.TestCase):
    def test_closed_fork_and_latest_run(self):
        config = {"repo": "owner/repo", "pr": 2}
        pr = {
            "state": "open",
            "draft": False,
            "head": {"sha": B, "repo": {"full_name": "owner/repo"}},
        }
        runs = [
            {
                "id": 1,
                "head_sha": B,
                "path": ".github/workflows/ci.yml",
                "status": "completed",
                "conclusion": "success",
                "run_attempt": 1,
            },
            {
                "id": 2,
                "head_sha": B,
                "path": ".github/workflows/ci.yml",
                "status": "in_progress",
                "conclusion": None,
                "run_attempt": 1,
            },
        ]
        with patch.object(preview, "api", side_effect=[pr, {"workflow_runs": runs}]):
            self.assertIsNone(preview.eligible(config)[0])
        for bad in (
            dict(pr, state="closed"),
            dict(pr, draft=True),
            dict(pr, head={"sha": B, "repo": {"full_name": "fork/repo"}}),
        ):
            with patch.object(preview, "api", return_value=bad):
                self.assertIsNone(preview.eligible(config)[0])
        runs[1].update(status="completed", conclusion="success")
        for names, success in (
            (["backend", "web"], False),
            (["backend", "web", "compose-smoke"], True),
        ):
            with patch.object(
                preview,
                "api",
                side_effect=[
                    pr,
                    {"workflow_runs": runs},
                    {"jobs": [{"name": n, "conclusion": "success"} for n in names]},
                ],
            ):
                self.assertEqual(preview.eligible(config)[0] is not None, success)

    def test_group2_ci_binding_is_explicit_and_legacy_shape_is_unchanged(self):
        config = {"repo": "owner/repo", "pr": 2}
        pr = {
            "state": "open",
            "draft": False,
            "head": {"sha": B, "repo": {"full_name": "owner/repo"}},
        }
        run = {
            "id": 9,
            "head_sha": B,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 2,
        }
        jobs = [
            {"id": index, "name": name, "conclusion": "success"}
            for index, name in enumerate(
                ("backend", "web", "compose-smoke", "local-preview"), 1
            )
        ]
        responses = [pr, {"workflow_runs": [run]}, {"jobs": jobs}]
        with patch.object(preview, "api", side_effect=responses):
            legacy, _ = preview.eligible(config)
        self.assertEqual(set(legacy), {"sha", "run_id", "run_attempt", "pr"})
        responses = [pr, {"workflow_runs": [run]}, {"jobs": jobs}]
        with patch.object(preview, "api", side_effect=responses):
            group2, _ = preview.eligible(config, preview.GROUP2_REQUIRED_JOBS)
        self.assertEqual(group2["required_jobs"], sorted(preview.GROUP2_REQUIRED_JOBS))
        self.assertEqual(group2["ci_binding"]["head_sha"], B)
        self.assertEqual(
            [item["name"] for item in group2["ci_binding"]["jobs"]],
            sorted(preview.GROUP2_REQUIRED_JOBS),
        )
        jobs[-1]["conclusion"] = "failure"
        responses = [pr, {"workflow_runs": [run]}, {"jobs": jobs}]
        with patch.object(preview, "api", side_effect=responses):
            self.assertIsNone(
                preview.eligible(config, preview.GROUP2_REQUIRED_JOBS)[0]
            )


class Group2ControllerGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        patcher = patch.object(preview, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, preview, "_group2_install_context", None)

    def test_install_guard_runs_only_under_nested_real_locks_and_flag_recovers(self):
        context = {"operation_held": False}
        preview._group2_install_context = context
        with patch.object(preview, "validate_group2_install_locked") as guard:
            with preview.config_lock():
                self.assertFalse(context["operation_held"])
            guard.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with preview.operation_lock():
                    self.assertTrue(context["operation_held"])
                    with preview.config_lock():
                        guard.assert_called_once_with(context)
                        raise RuntimeError("stop")
            self.assertFalse(context["operation_held"])

    def test_consumption_is_no_overwrite_and_fsyncs_real_files(self):
        active = self.root / "carry-forward" / "manifests"
        active.mkdir(parents=True, mode=0o700)
        source = active / ("1" * 32 + ".json")
        source.write_text("first\n")
        source.chmod(0o600)
        preview.consume_manifest(source)
        consumed = self.root / "carry-forward" / "consumed" / source.name
        self.assertEqual(consumed.read_text(), "first\n")
        self.assertFalse(source.exists())
        source.write_text("second\n")
        source.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "already consumed"):
            preview.consume_manifest(source)
        self.assertEqual(consumed.read_text(), "first\n")

    def test_receipt_requires_every_post_preservation_stage(self):
        manifest = {
            "manifest_id": "1" * 32,
            "manifest_digest": "2" * 64,
            "candidate_image_ids": {"image": "sha256:" + "3" * 64},
            "ci_binding": {"head_sha": B},
            "review_scope_digest": "4" * 64,
            "account_entry": {"profile": "safe-private"},
        }
        target = {"sha": B, "schema": "0040_issue152_dispositions"}
        stage_names = (
                    "preflight",
                    "control_stopped",
                    "stopped",
                    "backup",
                    "same_schema",
                    "started",
                    "probe",
                    "natural_cleanup",
                    "post_preservation",
                    "post_health",
                )
        account = {"profile": "safe-private"}
        receipt = {
            "mode": carry_forward.GROUP2_MODE,
            "sha": B,
            "schema": target["schema"],
            "images": manifest["candidate_image_ids"],
            "probe": {
                "status": "succeeded",
                "workspace_cleanup_status": "completed",
                "execution_id": 7,
            },
            "backup": "/private/backup",
            "carry_forward": {
                "manifest_id": manifest["manifest_id"],
                "manifest_digest": manifest["manifest_digest"],
            },
            "ci_binding": manifest["ci_binding"],
            "review_scope_digest": manifest["review_scope_digest"],
            "stages": {},
            "post_preservation_digest": "",
            "account_entry_digest": "6" * 64,
            "account_entry": account,
            "account_ready": True,
        }
        evidence = {name: {"stage": name} for name in stage_names}
        evidence["probe"] = receipt["probe"]
        receipt["stages"] = {
            name: carry_forward.digest(evidence[name]) for name in stage_names
        }
        receipt["post_preservation_digest"] = receipt["stages"][
            "post_preservation"
        ]
        with patch.object(
            carry_forward,
            "validate_group2_account_entry",
            return_value={"profile_digest": "6" * 64},
        ):
            safe = preview.validate_group2_receipt(receipt, target, manifest, evidence)
        self.assertNotIn("account_entry", safe)
        self.assertNotIn("backup", safe)
        self.assertTrue(safe["carry_forward"]["account_ready"])
        receipt["stages"].pop("natural_cleanup")
        with (
            patch.object(
                carry_forward,
                "validate_group2_account_entry",
                return_value={"profile_digest": "6" * 64},
            ),
            self.assertRaisesRegex(RuntimeError, "preservation stage"),
        ):
            preview.validate_group2_receipt(receipt, target, manifest, evidence)
        receipt["stages"]["natural_cleanup"] = carry_forward.digest(
            evidence["natural_cleanup"]
        )
        changed_evidence = copy.deepcopy(evidence)
        changed_evidence["post_health"]["stage"] = "unbound"
        with (
            patch.object(
                carry_forward,
                "validate_group2_account_entry",
                return_value={"profile_digest": "6" * 64},
            ),
            self.assertRaisesRegex(RuntimeError, "receipt evidence digest"),
        ):
            preview.validate_group2_receipt(
                receipt, target, manifest, changed_evidence
            )

    def test_review_artifacts_are_exact_private_regular_files(self):
        scope_path = self.root / "review-scope.json"
        scope_path.write_text("{}")
        scope_path.chmod(0o600)
        directory = self.root / "review-scope.evidence"
        request = b"approved request\n"
        product_anchor = {
            "base_sha": A,
            "head_sha": B,
            "base_tree": "1" * 40,
            "head_tree": "2" * 40,
            "raw_diff_sha256": "3" * 64,
            "files": ["backend/example.py"],
        }
        user_approval = {
            "status": "USER_APPROVED",
            "user_reply": "批准",
            "request_sha256": hashlib.sha256(request).hexdigest(),
            "product_candidate_sha": B,
        }
        historical_name = "group2-product-integration-recheck.md"
        historical_commit = "03b8fb196d5fa4da0df344e8e077e4a655e6a58c"
        historical_report = b"independent historical review\n"
        historical_digest = hashlib.sha256(historical_report).hexdigest()
        review_bindings = {
            "reviews_sha256": {historical_name: historical_digest},
            "integration_review_bound_commit": historical_commit,
            "ci_test_review_bound_commit": "4" * 40,
            "harness_review_bound_commit": "5" * 40,
        }
        blob_oid = "a" * 40
        scope_ci = {
            "head_sha": B,
            "run_id": 9,
            "run_attempt": 1,
            "workflow_path": ".github/workflows/ci.yml",
            "event": "pull_request",
            "jobs": [{"id": 1, "name": "backend", "conclusion": "success"}],
        }
        ci_value = {
            "run": {
                "id": 9, "head_sha": B, "run_attempt": 1,
                "path": ".github/workflows/ci.yml", "event": "pull_request",
                "status": "completed", "conclusion": "success",
            },
            "jobs": {
                "total_count": 1,
                "jobs": [{
                    "id": 1, "name": "backend", "status": "completed",
                    "conclusion": "success", "run_id": 9, "run_attempt": 1,
                    "head_sha": B, "steps": [],
                }],
            },
        }
        lineage = [{"name": "p1", "sha256": "b" * 64}]
        preservation_value = {
            "schema": "group2-preservation-review-v1",
            "status": "APPROVED",
            "source_kind": "private_snapshot",
            "snapshot_digest": "c" * 64,
            "lineage": lineage,
        }
        reviewed_bytes = b"reviewed final bytes\n"
        machine_review = {
            "schema": "group2-independent-review-v1",
            "status": "APPROVED",
            "reviewed_commit": B,
            "source_kind": "git_commit",
            "coverage": [{
                "path": "backend/example.py", "mode": "100644",
                "blob_oid": blob_oid,
                "sha256": hashlib.sha256(reviewed_bytes).hexdigest(),
            }],
            "blocking_findings": [],
        }
        contents = {
            "approval/REQUEST-ready.md": request,
            "approval/USER-APPROVAL.json": json.dumps(user_approval).encode(),
            "approval/product-scope.json": json.dumps(product_anchor).encode(),
            "approval/review-bindings.json": json.dumps(review_bindings).encode(),
            "ci/ci": json.dumps(ci_value).encode(),
            "preservation/preservation": ("```json\n" + json.dumps(preservation_value) + "\n```\n").encode(),
            "reviews/review": historical_report,
            "reviews/current": ("```json\n" + json.dumps(machine_review) + "\n```\n").encode(),
        }
        digests = {}
        for name, data in contents.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            path.write_bytes(data)
            path.chmod(0o600)
            digests[name] = hashlib.sha256(data).hexdigest()
        directory.chmod(0o700)
        scope = {
            "approval": {
                "request_sha256": digests["approval/REQUEST-ready.md"],
                "user_approval_sha256": digests["approval/USER-APPROVAL.json"],
                "product_scope_sha256": digests["approval/product-scope.json"],
                "review_bindings_sha256": digests[
                    "approval/review-bindings.json"
                ],
            },
            "ci": {**scope_ci, "evidence_sha256": digests["ci/ci"]},
            "product_anchor": product_anchor,
            "final_source": {"to_sha": B},
            "preservation_reference": {
                "review_report_sha256": digests["preservation/preservation"],
                "snapshot_digest": "c" * 64,
                "snapshot": {"lineage": lineage},
            },
            "reviews": [
                {
                    "name": historical_name,
                    "report_sha256": digests["reviews/review"],
                    "reviewed_commit": historical_commit,
                    "coverage": [{"path": "backend/example.py", "blob_oid": blob_oid}],
                },
                {
                    "name": "group2-v4-controller-final-review.md",
                    "report_sha256": digests["reviews/current"],
                    "reviewed_commit": B,
                    "coverage": [{"path": "backend/example.py", "blob_oid": blob_oid}],
                },
            ],
        }
        (directory / "ci/ci").rename(directory / "ci" / digests["ci/ci"])
        (directory / "preservation/preservation").rename(
            directory / "preservation" / digests["preservation/preservation"]
        )
        (directory / "reviews/review").rename(
            directory / "reviews" / digests["reviews/review"]
        )
        (directory / "reviews/current").rename(
            directory / "reviews" / digests["reviews/current"]
        )
        approval_hashes = {
            key: scope["approval"][key]
            for key in preview.GROUP2_APPROVAL_HASHES
        }
        with (
            patch.object(preview, "GROUP2_APPROVAL_HASHES", approval_hashes),
            patch.object(
                preview,
                "GROUP2_HISTORICAL_REVIEWS",
                {historical_name: historical_digest},
            ),
            patch.object(
                preview,
                "git",
                return_value=f"100644 blob {blob_oid}\tbackend/example.py",
            ),
            patch.object(preview, "git_bytes", return_value=reviewed_bytes),
        ):
            self.assertEqual(
                preview.validate_group2_artifacts(scope, scope_path), directory
            )
        current_review = scope["reviews"][1]
        current_path = directory / "reviews" / current_review["report_sha256"]
        original_review = current_path.read_bytes()

        def rejected_machine_report(value, message):
            old_digest = current_review["report_sha256"]
            old_path = directory / "reviews" / old_digest
            changed = ("```json\n" + json.dumps(value) + "\n```\n").encode()
            new_digest = hashlib.sha256(changed).hexdigest()
            old_path.unlink()
            (directory / "reviews" / new_digest).write_bytes(changed)
            (directory / "reviews" / new_digest).chmod(0o600)
            current_review["report_sha256"] = new_digest
            try:
                with (
                    patch.object(preview, "GROUP2_APPROVAL_HASHES", approval_hashes),
                    patch.object(preview, "GROUP2_HISTORICAL_REVIEWS", {historical_name: historical_digest}),
                    patch.object(preview, "git", return_value=f"100644 blob {blob_oid}\tbackend/example.py"),
                    patch.object(preview, "git_bytes", return_value=reviewed_bytes),
                    self.assertRaisesRegex(ValueError, message),
                ):
                    preview.validate_group2_artifacts(scope, scope_path)
            finally:
                (directory / "reviews" / new_digest).unlink()
                (directory / "reviews" / old_digest).write_bytes(original_review)
                (directory / "reviews" / old_digest).chmod(0o600)
                current_review["report_sha256"] = old_digest

        duplicate_conclusion = (
            "```json\n" + json.dumps(machine_review) + "\n```\n"
            "```json\n" + json.dumps({**machine_review, "status": "CHANGES_REQUIRED"}) + "\n```\n"
        ).encode()
        duplicate_digest = hashlib.sha256(duplicate_conclusion).hexdigest()
        current_path.unlink()
        duplicate_path = directory / "reviews" / duplicate_digest
        duplicate_path.write_bytes(duplicate_conclusion)
        duplicate_path.chmod(0o600)
        current_review["report_sha256"] = duplicate_digest
        with (
            patch.object(preview, "GROUP2_APPROVAL_HASHES", approval_hashes),
            patch.object(preview, "GROUP2_HISTORICAL_REVIEWS", {historical_name: historical_digest}),
            patch.object(preview, "git", return_value=f"100644 blob {blob_oid}\tbackend/example.py"),
            patch.object(preview, "git_bytes", return_value=reviewed_bytes),
            self.assertRaisesRegex(ValueError, "machine approved"),
        ):
            preview.validate_group2_artifacts(scope, scope_path)
        duplicate_path.unlink()
        current_path.write_bytes(original_review)
        current_path.chmod(0o600)
        current_review["report_sha256"] = hashlib.sha256(original_review).hexdigest()
        duplicate_coverage = copy.deepcopy(machine_review)
        duplicate_coverage["coverage"].append(copy.deepcopy(duplicate_coverage["coverage"][0]))
        rejected_machine_report(duplicate_coverage, "coverage changed")
        preservation = scope["preservation_reference"]
        preservation_path = directory / "preservation" / preservation["review_report_sha256"]
        original_preservation = preservation_path.read_bytes()
        contradictory_preservation = (
            original_preservation
            + ("```json\n" + json.dumps({**preservation_value, "status": "CHANGES_REQUIRED"}) + "\n```\n").encode()
        )
        changed_preservation_digest = hashlib.sha256(contradictory_preservation).hexdigest()
        preservation_path.unlink()
        changed_preservation_path = directory / "preservation" / changed_preservation_digest
        changed_preservation_path.write_bytes(contradictory_preservation)
        changed_preservation_path.chmod(0o600)
        preservation["review_report_sha256"] = changed_preservation_digest
        with (
            patch.object(preview, "GROUP2_APPROVAL_HASHES", approval_hashes),
            patch.object(preview, "GROUP2_HISTORICAL_REVIEWS", {historical_name: historical_digest}),
            patch.object(preview, "git", return_value=f"100644 blob {blob_oid}\tbackend/example.py"),
            patch.object(preview, "git_bytes", return_value=reviewed_bytes),
            self.assertRaisesRegex(ValueError, "preservation review"),
        ):
            preview.validate_group2_artifacts(scope, scope_path)
        changed_preservation_path.unlink()
        preservation_path.write_bytes(original_preservation)
        preservation_path.chmod(0o600)
        preservation["review_report_sha256"] = hashlib.sha256(original_preservation).hexdigest()
        approval_file = directory / "approval/USER-APPROVAL.json"
        original_approval = approval_file.read_bytes()
        contradictory = {**user_approval, "product_candidate_sha": A}
        approval_file.write_text(json.dumps(contradictory))
        contradictory_digest = hashlib.sha256(approval_file.read_bytes()).hexdigest()
        scope["approval"]["user_approval_sha256"] = contradictory_digest
        contradictory_hashes = {**approval_hashes, "user_approval_sha256": contradictory_digest}
        with (
            patch.object(preview, "GROUP2_APPROVAL_HASHES", contradictory_hashes),
            patch.object(preview, "GROUP2_HISTORICAL_REVIEWS", {historical_name: historical_digest}),
                patch.object(preview, "git", return_value=f"100644 blob {blob_oid}\tbackend/example.py"),
                patch.object(preview, "git_bytes", return_value=reviewed_bytes),
                self.assertRaisesRegex(ValueError, "contradicts"),
        ):
            preview.validate_group2_artifacts(scope, scope_path)
        approval_file.write_bytes(original_approval)
        scope["approval"]["user_approval_sha256"] = approval_hashes[
            "user_approval_sha256"
        ]
        request = directory / "approval/REQUEST-ready.md"
        request.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private regular"):
            with (
                patch.object(preview, "GROUP2_APPROVAL_HASHES", approval_hashes),
                patch.object(preview, "GROUP2_HISTORICAL_REVIEWS", {historical_name: historical_digest}),
                patch.object(preview, "git", return_value=f"100644 blob {blob_oid}\tbackend/example.py"),
                patch.object(preview, "git_bytes", return_value=reviewed_bytes),
            ):
                preview.validate_group2_artifacts(scope, scope_path)
        request.chmod(0o600)
        request.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "digest changed"):
            with (
                patch.object(preview, "GROUP2_APPROVAL_HASHES", approval_hashes),
                patch.object(preview, "GROUP2_HISTORICAL_REVIEWS", {historical_name: historical_digest}),
                patch.object(preview, "git", return_value=f"100644 blob {blob_oid}\tbackend/example.py"),
                patch.object(preview, "git_bytes", return_value=reviewed_bytes),
            ):
                preview.validate_group2_artifacts(scope, scope_path)

    def test_deploy_executes_full_group2_flow_and_commit_failure_stays_unready(self):
        def fixture(name, fail_commit=False):
            root = self.root / name
            root.mkdir(mode=0o700)
            shutil.copy2(Path(preview.__file__).with_name("deploy.sh"), root / "deploy.sh")
            (root / "prepare-sandbox-host.sh").write_text("#!/bin/sh\nexit 0\n")
            (root / "prepare-sandbox-host.sh").chmod(0o755)
            (root / "verify.py").write_text("# synthetic official probe\n")
            (root / "assets.py").write_text("# synthetic assets\n")
            (root / "deployment.json").write_text(json.dumps({
                "project": "example", "web_port": 18080,
                "sandbox_unit": "example.service", "sandbox_cpu_quota": "100%",
                "sandbox_memory_max": "1G",
            }))
            (root / "preview.env").write_text("DLR_ADMIN_TOKEN=synthetic\n")
            old_sha, sha, manifest_id = A, B, "a" * 32
            old_images = {
                f"example-{service}:{old_sha}": "sha256:" + str(index) * 64
                for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
            }
            images = {
                f"example-{service}:{sha}": "sha256:" + str(index + 4) * 64
                for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
            }
            for release_sha, release_images in ((old_sha, old_images), (sha, images)):
                release = root / "releases" / release_sha
                release.mkdir(parents=True)
                (release / ".downloaded").touch()
                (release / "docker-compose.yml").write_text("services: {}\n")
                (release / "compose.preview.json").write_text("{}\n")
                (release / "images.json").write_text(json.dumps(release_images))
            (root / "current-sha").write_text(old_sha + "\n")
            profile = {
                "profile_digest": "9" * 64,
                "old_containers": {"worker": {"container_id": "w" * 40}},
            }
            baseline_log = {
                "profile_digest": profile["profile_digest"], "files": [], "roots": [],
                "observed_at_ns": 1,
            }
            baseline_log["evidence_digest"] = carry_forward.digest(baseline_log)
            controller = {
                filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
                for filename in ("deploy.sh", "verify.py", "assets.py")
            }
            baseline_db = {
                key: {} for key in (
                    "projection", "responsibilities", "protected_rows",
                    "asset_projection", "schema_shape", "schema_inventory",
                )
            }
            baseline_files = {"fixture": "preserved"}
            containers = []
            for service in ("postgres", "rabbitmq", "control", "worker"):
                containers.append({
                    "service": service, "container_id": (service[0] * 40),
                    "image_id": old_images.get(
                        f"example-{service}:{old_sha}", "sha256:" + "8" * 64
                    ),
                    "labels": {"com.docker.compose.project": "example",
                               "com.docker.compose.service": service},
                    **({"runtime_config": {}} if service == "worker" else {}),
                })
            containers.sort(key=lambda item: item["service"])
            manifest = {
                "format_version": carry_forward.GROUP2_FORMAT_VERSION,
                "mode": carry_forward.GROUP2_MODE, "from_sha": old_sha, "to_sha": sha,
                "to_schema": "0040_issue152_dispositions", "manifest_id": manifest_id,
                "manifest_digest": "b" * 64, "review_scope_digest": "c" * 64,
                "candidate_image_ids": images, "old_image_ids": old_images,
                "storage_identity": [], "old_containers": containers,
                "selection": {}, "account_entry": profile, "log_evidence": baseline_log,
                "file_evidence": baseline_files,
                "old_runtime_projection": {}, "responsibilities": {},
                "protected_rows": {}, "asset_projection": {}, "schema_shape": {},
                "ci_binding": {"head_sha": sha},
                "review_scope": {"controller_files": {"files": controller}},
            }
            manifest_dir = root / "carry-forward" / "manifests"
            manifest_dir.mkdir(parents=True, mode=0o700)
            (manifest_dir / f"{manifest_id}.json").write_text(json.dumps(manifest))
            state = {
                "db": baseline_db, "files": baseline_files, "images": images,
                "old_images": old_images, "old_sha": old_sha, "sha": sha,
            }
            (root / "fixture.json").write_text(json.dumps(state))
            (root / "carry_forward.py").write_text(
                """#!/usr/bin/env python3
import hashlib,json,os,pathlib,sys,time
GROUP2_FORMAT_VERSION=4; GROUP2_MODE='audited-group2-same-schema-v1'
def digest(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def read_private(p): return json.load(open(p))
def write_private(p,v):
 p=pathlib.Path(p); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(v)); p.chmod(0o600)
def validate_manifest(v): return v
def validate_group2_manifest_extensions(v): return None
def validate_storage_identity(v): return v
def _worker_runtime_config(v): return {}
def compare_projection(a,b):
 assert a==b
def compare_group2_startup_files(a,b,p): return {'code':'startup-ok'}
def compare_group2_post_probe(*a): return {'code':'post-ok'}
def runtime(req):
 op=req['operation']; profile=req.get('profile',{'profile_digest':'9'*64})
 if op=='account-capture': return {'account_entry':json.load(open(__file__.replace('carry_forward.py','carry-forward/manifests/'+'a'*32+'.json')))['account_entry']}
 if op=='account-check':
  csrf={'status':200,'body_status':'ok','csrf_cookie':True,'csrf_cookie_path':True,'csrf_cookie_samesite_lax':True,'csrf_cookie_httponly':False,'redirect':False}
  worker={'container_id':'f'*40,'image_id':'sha256:'+'7'*64,'status':'running','health':'healthy','started_at':'2026-09-20T00:00:00Z','restart_count':0,'command':[None,['run']],'labels':{},'port_bindings':{},'mounts':[],'networks':['example_default']}
  return {'account_check':{'profile_digest':profile['profile_digest'],'containers':{'worker':worker},'account_csrf':csrf}}
 if op=='log-capture':
  value={'profile_digest':profile['profile_digest'],'files':[],'roots':[],'observed_at_ns':time.time_ns()}; value['evidence_digest']=digest(value); return {'log_evidence':value}
 if op=='log-append':
  base=req['baseline']; value={'profile_digest':base['profile_digest'],'files':[],'roots':base['roots'],'observed_after_ns':time.time_ns(),'baseline_evidence_digest':base['evidence_digest']}; value['evidence_digest']=digest(value); return {'log_evidence':value}
 if op=='startup-proof': return {'startup_proof':{'code':'startup-ok'}}
 if op=='entry-probe': return {'entry_probe':{'code':'entry-ok'}}
 if op=='probe-proof': return {'probe_proof':{'probe_result':req['probe_result'],'code':'probe-ok'}}
 if op=='probe-cleanup': return {'cleanup':{'status':'completed','residue':False}}
 raise SystemExit(2)
if __name__=='__main__':
 a=sys.argv[1:]
 if a[0]=='group2-runtime':
  req=json.load(open(a[a.index('--request')+1])); write_private(a[a.index('--output')+1],runtime(req))
 elif a[0]=='check-kernel': write_private(a[a.index('--output')+1],{'code':'kernel-ok'})
 else: raise SystemExit(2)
"""
            )
            manifest_path = manifest_dir / f"{manifest_id}.json"
            persisted_manifest = json.loads(manifest_path.read_text())
            persisted_manifest["review_scope"]["controller_files"]["files"][
                "carry_forward.py"
            ] = hashlib.sha256((root / "carry_forward.py").read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(persisted_manifest))
            fake = root / "bin"
            fake.mkdir()
            (fake / "curl").write_text("#!/bin/sh\nexit 0\n")
            (fake / "curl").chmod(0o755)
            (fake / "flock").write_text("#!/bin/sh\nexit 0\n")
            (fake / "flock").chmod(0o755)
            (fake / "mv").write_text(
                "#!/bin/sh\n"
                + ("case \"${@: -1}\" in */current-sha) exit 97;; esac\n" if fail_commit else "")
                + "exec /bin/mv \"$@\"\n"
            )
            (fake / "mv").chmod(0o755)
            (fake / "docker").write_text(
                """#!/usr/bin/env python3
import json,pathlib,sys
root=pathlib.Path(__file__).parent.parent; state=json.load(open(root/'fixture.json')); a=sys.argv[1:]
with (root/'events').open('a') as out: out.write(' '.join(a)+'\\n')
def service(name): return next((s for s in ('rabbitmq','postgres','control','worker','account-web','web') if name==f'example-{s}-1'),'worker')
if a[0]=='image' and a[1]=='inspect':
 key=a[2]; values={**state['images'],**state['old_images']}
 if key.startswith('sha256:'): print(json.dumps({'User':'0','Env':[]})) if '{{json .Config}}' in a[-1] else print(key)
 else: print(values[key])
elif a[0]=='volume': print(a[2] if '{{.Name}}' in a[-1] else 'example')
elif a[0]=='ps': pass
elif a[0]=='exec':
 if 'pg_dump' in a: print('synthetic-dump')
 elif 'pg_restore' in a: print('synthetic-list')
 else: print('0040_issue152_dispositions')
elif a[0]=='inspect':
 s=service(a[1]); old=state['old_images']; current=state['images'] if state.get('started') else old
 tag_service='web' if s=='account-web' else s
 image=current.get(f"example-{tag_service}:{state['sha'] if state.get('started') else state['old_sha']}",'sha256:'+'8'*64)
 if '--format' not in a: print(json.dumps([{'Config':{'Env':['DATABASE_URL=postgresql://synthetic']},'NetworkSettings':{'Networks':{'example_default':{}}}}]))
 else:
  fmt=a[-1]
  if 'Config.User' in fmt: print('0:0')
  elif 'eq .Destination' in fmt and '/runtime' in fmt: print('volume runtime')
  elif 'eq .Destination' in fmt and '/journal' in fmt: print('volume journal')
  elif 'ne .Destination' in fmt: print('/var/lib/dlr/artifacts' if '.Destination' in fmt and '{{.Name}}' not in fmt else 'volume artifacts')
  elif 'eq .Destination' in fmt: print('volume builtin')
  elif '{{json .Mounts}}' in fmt: print('[]')
  elif '{{json .Config}}' in fmt: print('{}')
  elif '.State.Running' in fmt: print('false')
  elif '.State' in fmt: print('running')
  elif '.Id' in fmt: print((s[0] or 'x')*40)
  elif '.Image' in fmt: print(image)
  elif 'compose.project' in fmt: print('example')
  elif 'compose.service' in fmt: print(s)
  elif 'CgroupnsMode' in fmt: print('private:false')
  else: print('')
elif a[0]=='run':
 source=None
 for i,v in enumerate(a):
  if v=='--mount':
   fields=dict(x.split('=',1) for x in a[i+1].split(',') if '=' in x)
   if fields.get('target')=='/evidence': source=pathlib.Path(fields['source'])
 if 'capture-state' in a:
  (source/pathlib.Path(a[a.index('--db-output')+1]).name).write_text(json.dumps(state['db']))
  (source/pathlib.Path(a[a.index('--files-output')+1]).name).write_text(json.dumps(state['files']))
 elif 'group2-runtime' in a:
  req=source/pathlib.Path(a[a.index('--request')+1]).name; output=source/pathlib.Path(a[a.index('--output')+1]).name
  request=json.load(open(req)); output.write_text(json.dumps({'cleanup':{'status':'completed','residue':False}}))
 print('{}')
elif a[0]=='compose':
 if 'config' in a: print(json.dumps({'services':{s:{'image':f"example-{s}:{state['sha']}",'environment':{},'volumes':[]} for s in ('postgres','rabbitmq','control','worker')},'volumes':{}}))
 elif 'up' in a and '-f' in a and 'docker-compose.yml' in a:
  state['started']=True; (root/'fixture.json').write_text(json.dumps(state)); print('ok')
 elif 'ps' in a and '-q' in a: print('example-'+a[-1]+'-1')
 elif 'exec' in a and 'control' in a: print(json.dumps({'status':'succeeded','workspace_cleanup_status':'completed','execution_id':7}))
 elif 'run' in a and 'assets.py' in a: print('{}')
 else: print('ok')
else: print('ok')
"""
            )
            (fake / "docker").chmod(0o755)
            env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
            result = subprocess.run(
                [str(root / "deploy.sh"), sha, "deploy", "0040_issue152_dispositions", manifest_id],
                cwd=root, env=env, text=True, capture_output=True, check=False,
            )
            return root, result

        root, result = fixture("deploy-happy")
        self.assertEqual(
            result.returncode,
            0,
            result.stderr + result.stdout + (root / "events").read_text(),
        )
        transaction = json.loads((root / "transaction.json").read_text())
        self.assertEqual(transaction["phase"], "ready")
        receipt = json.loads((root / "releases" / B / "receipt.json").read_text())
        self.assertEqual(set(receipt["stages"]), {
            "preflight", "control_stopped", "stopped", "backup", "same_schema",
            "started", "probe", "natural_cleanup", "post_preservation", "post_health",
        })
        events = (root / "events").read_text().splitlines()
        self.assertEqual(sum("compose" in event and "exec -T control python -" in event for event in events), 1)
        self.assertLess(next(i for i,e in enumerate(events) if "stop control" in e),
                        next(i for i,e in enumerate(events) if "pg_dump" in e))
        self.assertLess(next(i for i,e in enumerate(events) if "alembic upgrade head" in e),
                        next(i for i,e in enumerate(events) if "exec -T control python -" in e))
        failed_root, failed = fixture("deploy-failed-commit", fail_commit=True)
        self.assertEqual(failed.returncode, 97, failed.stderr)
        self.assertEqual((failed_root / "current-sha").read_text().strip(), A)
        self.assertEqual(json.loads((failed_root / "transaction.json").read_text())["phase"], "committing")

    def test_recover_executes_four_app_flow_and_never_runs_official_probe(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, code=None, cookie=False):
                self.send_response(status)
                if cookie:
                    self.send_header(
                        "Set-Cookie", "dlr_account_csrf=value; Path=/; SameSite=Lax"
                    )
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                body = {"status": "ok"} if code is None else {"detail": {"code": code}}
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                account = self.server.account
                if account and self.path == "/api/auth/account/csrf":
                    self.reply(200, cookie=True)
                elif not account and self.path == "/api/auth/account/csrf":
                    self.reply(401, "account_entry_required")
                elif account and self.path == "/api/auth/admin/verify":
                    self.reply(401, "token_entry_required")
                elif not account and self.path == "/api/auth/admin/verify":
                    authorization = self.headers.get("Authorization")
                    if authorization == "Bearer test-token":
                        self.reply(200)
                    else:
                        self.reply(401, "unauthorized")
                elif account and self.path == "/api/auth/account/me":
                    self.reply(401, "account_session_required")
                else:
                    self.reply(404, "missing")

            def do_POST(self):
                if self.server.account and self.path == "/api/auth/account/logout":
                    self.reply(403, "account_csrf_invalid")
                else:
                    self.reply(404, "missing")

        servers = []
        for account in (True, False):
            server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
            server.account = account
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        account_port, token_port = (server.server_address[1] for server in servers)

        root = self.root / "vm"
        root.mkdir(mode=0o700)
        shutil.copy2(Path(preview.__file__).with_name("deploy.sh"), root / "deploy.sh")
        shutil.copy2(Path(carry_forward.__file__), root / "carry_real.py")
        (root / "carry_forward.py").write_text(
            """#!/usr/bin/env python3
import importlib.util, pathlib
_spec=importlib.util.spec_from_file_location('carry_real',pathlib.Path(__file__).with_name('carry_real.py'))
_real=importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_real)
for _name in dir(_real):
    if not _name.startswith('_'): globals()[_name]=getattr(_real,_name)
_inspect_container = _real._inspect_container
def validate_manifest(value): return value
def validate_group2_manifest_extensions(value): return None
def compare_group2_startup_files(before, after, proof):
    _real._validate_startup_proof(proof)
    return {'code':'group2_startup_files_ok','allowed_deltas':[]}
if __name__ == '__main__': _real.main()
"""
        )
        (root / "prepare-sandbox-host.sh").write_text("#!/bin/sh\nexit 0\n")
        (root / "prepare-sandbox-host.sh").chmod(0o755)
        (root / "deployment.json").write_text(
            json.dumps(
                {
                    "project": "example",
                    "web_port": token_port,
                    "sandbox_unit": "example.service",
                    "sandbox_cpu_quota": "100%",
                    "sandbox_memory_max": "1G",
                }
            )
        )
        (root / "preview.env").write_text(
            f"DLR_ADMIN_TOKEN=test-token\nDLR_WEB_HOST_PORT={token_port}\n"
            f"DLR_ACCOUNT_WEB_HOST_PORT={account_port}\n"
        )
        for path in (root / "deployment.json", root / "preview.env"):
            path.chmod(0o600)
        sha = B
        release = root / "releases" / sha
        release.mkdir(parents=True)
        (release / ".downloaded").touch()
        (release / "docker-compose.yml").write_text("services: {}\n")
        (release / "schema").write_text("0040_issue152_dispositions\n")
        images = {
            f"example-{service}:{sha}": "sha256:" + str(index) * 64
            for index, service in enumerate(("postgres", "control", "worker", "web"), 1)
        }
        (release / "images.json").write_text(json.dumps(images))
        log_roots = {}
        log_files = []
        for service in ("control", "worker", "web", "account-web"):
            service_root = self.root / "logs" / service
            service_root.mkdir(parents=True)
            names = (
                ("access.log", "error.log")
                if service in {"web", "account-web"}
                else (f"{service}.log",)
            )
            for name in names:
                path = service_root / name
                path.write_text("")
                log_files.append(str(path))
            log_roots[service] = {
                "path": str(service_root),
                "allowed_new_files": sorted(names),
            }
        worker_log = self.root / "logs" / "worker" / "worker.log"

        binding = {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(account_port)}]
        }
        token_binding = {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(token_port)}]
        }
        def fact(service, image):
            mounts = []
            if service in log_roots:
                mounts = [{
                    "type": "bind",
                    "source": log_roots[service]["path"],
                    "destination": f"/var/lib/dlr/platform-logs/{service}",
                    "rw": True,
                }]
            return {
                "container_id": (service[0] if service else "a") * 64,
                "image_id": image,
                "status": "running",
                "health": "healthy",
                "started_at": "2026-09-20T00:00:00Z",
                "restart_count": 0,
                "command": [None, ["run"]],
                "labels": {
                    "com.docker.compose.project": "example",
                    "com.docker.compose.service": service,
                },
                "port_bindings": (
                    binding
                    if service == "account-web"
                    else token_binding
                    if service == "web"
                    else {}
                ),
                "mounts": mounts,
                "networks": ["example_default"],
            }
        actual = {
            service: fact(
                service,
                images[f"example-{'web' if service == 'account-web' else service}:{sha}"],
            )
            for service in ("control", "worker", "web", "account-web")
        }
        actual["postgres"] = fact("postgres", images[f"example-postgres:{sha}"])
        old = copy.deepcopy(
            {key: actual[key] for key in ("control", "worker", "web", "account-web")}
        )
        old["account-web"]["image_id"] = "sha256:" + "9" * 64
        candidate_profiles = {}
        for service in ("control", "worker", "web", "account-web"):
            ports = []
            if service == "web":
                ports = [{"host_ip": "127.0.0.1", "published": str(token_port), "target": 80, "protocol": "tcp"}]
            elif service == "account-web":
                ports = [{"host_ip": "127.0.0.1", "published": str(account_port), "target": 80, "protocol": "tcp"}]
            candidate_profiles[service] = {
                "image": f"example-{'web' if service == 'account-web' else service}:{sha}",
                "command": ["run"],
                "effective_command": [None, ["run"]],
                "networks": ["example_default"],
                "mounts": actual[service]["mounts"],
                "ports": ports,
            }
        profile = {
            "project": "example",
            "from_sha": A,
            "to_sha": sha,
            "old_containers": old,
            "old_profiles": copy.deepcopy(candidate_profiles),
            "candidate_profiles": candidate_profiles,
            "candidate_web_image_id": images[f"example-web:{sha}"],
            "candidate_image_ids_by_service": {
                service: images[f"example-{'web' if service == 'account-web' else service}:{sha}"]
                for service in ("control", "worker", "web", "account-web")
            },
            "ports": {
                "account": {
                    "host_ip": "127.0.0.1",
                    "published": str(account_port),
                    "target": 80,
                    "protocol": "tcp",
                },
                "token": [
                    {
                        "host_ip": "127.0.0.1",
                        "published": str(token_port),
                        "target": 80,
                        "protocol": "tcp",
                    }
                ],
            },
            "log_files": sorted(log_files),
            "log_roots": sorted(log_roots.values(), key=lambda item: item["path"]),
        }
        profile["profile_digest"] = carry_forward.digest(profile)
        http_result = carry_forward._http_result(
            f"http://127.0.0.1:{account_port}/api/auth/account/csrf"
        )
        self.assertEqual(http_result["status"], 200)
        self.assertEqual(http_result["body_status"], "ok")
        self.assertTrue(http_result["csrf_cookie"])
        self.assertTrue(http_result["csrf_cookie_path"])
        self.assertTrue(http_result["csrf_cookie_samesite_lax"])
        self.assertFalse(http_result["csrf_cookie_httponly"])
        manifest = {
            "from_sha": A,
            "to_sha": sha,
            "manifest_id": "a" * 32,
            "manifest_digest": "b" * 64,
            "review_scope_digest": "c" * 64,
        }
        manifest_dir = root / "carry-forward" / "manifests"
        manifest_dir.mkdir(parents=True, mode=0o700)
        (root / "carry-forward").chmod(0o700)
        manifest_path = manifest_dir / (manifest["manifest_id"] + ".json")
        manifest_path.write_text(
            json.dumps(manifest)
        )
        manifest_path.chmod(0o600)
        baseline_db = {
            key: {} for key in (
                "projection",
                "responsibilities",
                "protected_rows",
                "asset_projection",
                "schema_shape",
                "schema_inventory",
            )
        }
        baseline_files = {"synthetic": "preserved"}
        post_preservation = {"code": "group2_post_probe_ok"}
        post_bundle = {
            "result": post_preservation,
            "db": baseline_db,
            "files": baseline_files,
        }
        baseline = release / "recovery-baseline"
        baseline.mkdir()
        (baseline / "ids.json").write_text("{}")
        (baseline / "db.json").write_text(json.dumps(baseline_db))
        (baseline / "files.json").write_text(json.dumps(baseline_files))
        (baseline / "post-preservation.json").write_text(
            json.dumps(post_preservation)
        )
        bundle_digest = carry_forward.digest(post_bundle)
        (release / "receipt.json").write_text(
            json.dumps(
                {
                    "mode": carry_forward.GROUP2_MODE,
                    "account_entry": profile,
                    "carry_forward": {
                        "manifest_id": manifest["manifest_id"],
                        "manifest_digest": manifest["manifest_digest"],
                    },
                    "review_scope_digest": manifest["review_scope_digest"],
                    "post_preservation_digest": bundle_digest,
                    "stages": {"post_preservation": bundle_digest},
                }
            )
        )
        (root / "transaction.json").write_text(
            json.dumps({"phase": "ready", "sha": sha})
        )
        (root / "current-sha").write_text(sha + "\n")

        fake = self.root / "bin"
        fake.mkdir()
        facts = fake / "facts.json"
        facts.write_text(
            json.dumps(
                {
                    "containers": actual,
                    "images": images,
                    "baseline_db": baseline_db,
                    "baseline_files": baseline_files,
                    "worker_log": str(worker_log),
                }
            )
        )
        docker = fake / "docker"
        docker.write_text(
            """#!/usr/bin/env python3
import datetime, json, pathlib, sys
facts_path=pathlib.Path(__file__).parent/'facts.json'
data=json.loads(facts_path.read_text())
a=sys.argv[1:]
if a[0]=='exec':
 print('0040_issue152_dispositions')
elif a[0]=='inspect':
 name=a[1]
 service=next((s for s in ('account-web','postgres','control','worker','web') if name==f'example-{s}-1'),'worker')
 item=data['containers'][service]
 if '--format' not in a:
  raw={'Id':item['container_id'],'Image':item['image_id'],'RestartCount':0,
       'State':{'Status':'running','Health':{'Status':'healthy'},'StartedAt':item['started_at']},
       'Config':{'Entrypoint':None,'Cmd':['run'],'Labels':item['labels'],
                 'Env':['DATABASE_URL=postgresql://synthetic']},
       'HostConfig':{'PortBindings':item['port_bindings']},
       'Mounts':[{'Type':m['type'],'Source':m['source'],'Destination':m['destination'],'RW':m['rw']}
                 for m in item['mounts']],
       'NetworkSettings':{'Networks':{'example_default':{}}}}
  print(json.dumps([raw]))
 elif 'CgroupnsMode' in a[-1]: print('private:false')
 elif 'Config.User' in a[-1]: print('1000:1000')
 elif '.Mounts' in a[-1]:
  if 'ne .Destination' in a[-1]:
   print('volume example_artifacts' if '{{.Name}}' in a[-1] else '/var/lib/dlr/artifacts')
  elif '/var/lib/dlr/runtime' in a[-1]: print('volume example_runtime')
  elif '/var/lib/dlr/journal' in a[-1]: print('volume example_journal')
  else: print('volume example_builtin')
 else: print(item['image_id'])
elif a[:2]==['image','inspect']:
 print(data['images'][a[2]])
elif a[:2]==['volume','inspect']:
 if '{{.Name}}' in a[-1]: print(a[2])
 else: print('example')
elif a[0]=='run':
 source=None
 for index,value in enumerate(a):
  if value=='--mount':
   mount=dict(part.split('=',1) for part in a[index+1].split(',') if '=' in part)
   if mount.get('target')=='/evidence': source=pathlib.Path(mount['source'])
 if 'capture-state' in a:
  db=pathlib.Path(a[a.index('--db-output')+1]).name
  files=pathlib.Path(a[a.index('--files-output')+1]).name
  (source/db).write_text(json.dumps(data['baseline_db']))
  (source/files).write_text(json.dumps(data['baseline_files']))
 print('{}')
elif a[0]=='compose':
 if '--force-recreate' in a:
  data['containers']['worker']['container_id']='f'*64
  data['containers']['worker']['started_at']=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
  nonce='1234567890abcdef'
  capability_names=('adapter_control_plane_hidden','adapter_mount_blocked','bounded_output','cgroup_kill',
   'cgroup_namespace_private','cgroup_v2','cpu_hard_limit','memory_hard_limit','mount_namespace',
   'no_new_privileges','nofile_hard_limit','pid_namespace','pids_hard_limit','preflight_passed',
   'sandbox_cleanup','swap_hard_limit','tmpfs_hard_limit')
  cgroup='dlr-preflight-'+nonce
  receipt={'cgroup_name':cgroup,'status':'passed','workspace_residue':False,
   'cleanup':{'status':'completed','residue':False,'error_code':None,'cgroup_name':cgroup},
   'capabilities':{name:True for name in capability_names},'adapter_control_pipe_fds':[],
   'adapter_hidden_cgroup_paths':{'/run/dlr-cgroup':{'read_blocked':True,'write_blocked':True},
                                  '/sys/fs/cgroup':{'read_blocked':True,'write_blocked':True}},
   'agent_outside_attempt':True,'helper_outside_attempt':True,'probe_in_attempt':True,
   'child_empty_after_kill':True,'process_exited_after_kill':True,
   'worker_cgroup_management':{'child_limit_write_read':True,'parent_controllers_read':True},
   'namespace_identity':{'boot_id':'boot','parent_device':1,'parent_inode':2,'root_device':1,'root_inode':3}}
  with open(data['worker_log'],'a') as out:
   out.write('sandbox preflight receipt: '+json.dumps(receipt)+'\\n')
   out.write('sandbox preflight passed; rabbitmq execution gate=True\\n')
  facts_path.write_text(json.dumps(data))
 if 'ps' in a and '-q' in a: print('example-worker-1')
 else: print('ok')
else: print('ok')
"""
        )
        docker.chmod(0o755)
        for name in ("flock", "curl"):
            script = fake / name
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
        result = subprocess.run(
            [str(root / "deploy.sh"), sha, "recover"],
            env={**os.environ, "PATH": str(fake) + ":" + os.environ["PATH"]},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        recoveries = list((root / "carry-forward").glob(f"recovery-{sha}-*"))
        self.assertEqual(len(recoveries), 1)
        self.assertTrue((recoveries[0] / "entry.json").is_file())
        self.assertTrue((recoveries[0] / "preservation.json").is_file())
        self.assertFalse((release / "probe.json").exists())
        log_after_success = worker_log.read_bytes()
        (baseline / "db.json").write_text(json.dumps({"tampered": True}))
        rejected = subprocess.run(
            [str(root / "deploy.sh"), sha, "recover"],
            env={**os.environ, "PATH": str(fake) + ":" + os.environ["PATH"]},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(worker_log.read_bytes(), log_after_success)
        self.assertFalse((release / "probe.json").exists())


if __name__ == "__main__":
    unittest.main()
