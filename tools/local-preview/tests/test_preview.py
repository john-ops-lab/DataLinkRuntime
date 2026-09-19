import sys
import tempfile
import unittest
import json
import os
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


if __name__ == "__main__":
    unittest.main()
