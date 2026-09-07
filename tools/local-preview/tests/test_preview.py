import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preview
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
