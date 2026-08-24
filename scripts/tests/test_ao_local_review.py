from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import ao_local_review as sidecar  # noqa: E402
import ao_local_review_claude_wrapper as wrapper  # noqa: E402


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


class RepositoryFixture:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Test User")
        git(root, "config", "user.email", "test@example.com")
        git(
            root,
            "remote",
            "add",
            "origin",
            "https://github.com/john-ops-lab/DataLinkRuntime.git",
        )
        self.commit("README.md", "base\n", "base")
        self.base = git(root, "rev-parse", "HEAD")
        git(root, "switch", "-c", "feature")
        self.commit("feature.txt", "feature\n", "feature")

    def commit(self, path: str, content: str, message: str) -> str:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        git(self.root, "add", path)
        git(self.root, "commit", "-m", message)
        return git(self.root, "rev-parse", "HEAD")


def args_for(
    repo: pathlib.Path, data: pathlib.Path, **overrides: object
) -> argparse.Namespace:
    values: dict[str, object] = {
        "repo": str(repo),
        "worker_worktree": str(repo),
        "data_dir": str(data),
        "dispatch_id": "dispatch-1",
        "worker_session_id": "worker-1",
        "base": "main",
        "candidate": "HEAD",
        "model": "k3",
        "validation_file": [],
        "browser_evidence_file": [],
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class SidecarStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temporary.name)
        self.repo = base / "repo"
        self.repo.mkdir()
        self.fixture = RepositoryFixture(self.repo)
        self.data = base / "ao-data"
        self.paths = sidecar.Paths.resolve(args_for(self.repo, self.data))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_pins_candidate_outside_worker_checkout(self) -> None:
        validation = pathlib.Path(self.temporary.name) / "validation.json"
        candidate = git(self.repo, "rev-parse", "HEAD")
        validation.write_text(
            json.dumps({"candidate_sha": candidate, "tests": "passed"}),
            encoding="utf-8",
        )
        result = sidecar.command_start(
            args_for(self.repo, self.data, validation_file=[str(validation)]),
            self.paths,
        )

        request = result["request"]
        self.assertEqual(request["candidate_sha"], candidate)
        self.assertEqual(request["repository"], "john-ops-lab/DataLinkRuntime")
        self.assertEqual(request["round"], 1)
        self.assertFalse(self.paths.state_root.is_relative_to(self.repo))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        bundle = sidecar.load_json(
            pathlib.Path(result["round_dir"]) / "review-bundle.json"
        )
        self.assertEqual(bundle["validation_receipts"][0]["candidate_sha"], candidate)
        self.assertEqual(
            bundle["changed_files"], [{"path": "feature.txt", "status": "A"}]
        )

    def test_start_is_idempotent_for_same_candidate(self) -> None:
        first = sidecar.command_start(args_for(self.repo, self.data), self.paths)
        second = sidecar.command_start(args_for(self.repo, self.data), self.paths)
        self.assertEqual(first["round_dir"], second["round_dir"])
        self.assertEqual(second["status"], "existing")

    def test_start_rejects_dirty_worker(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(sidecar.SidecarError, "clean") as raised:
            sidecar.command_start(args_for(self.repo, self.data), self.paths)
        self.assertEqual(raised.exception.code, "WORKER_DIRTY")

    def test_start_rejects_non_k3_reviewer(self) -> None:
        with self.assertRaises(sidecar.SidecarError) as raised:
            sidecar.command_start(
                args_for(self.repo, self.data, model="claude-sonnet-4-6"), self.paths
            )
        self.assertEqual(raised.exception.code, "REVIEWER_MODEL_MISMATCH")

    def test_evidence_for_old_sha_is_rejected(self) -> None:
        evidence = pathlib.Path(self.temporary.name) / "browser.json"
        evidence.write_text(
            json.dumps({"candidate_sha": self.fixture.base}), encoding="utf-8"
        )
        with self.assertRaises(sidecar.SidecarError) as raised:
            sidecar.command_start(
                args_for(self.repo, self.data, browser_evidence_file=[str(evidence)]),
                self.paths,
            )
        self.assertEqual(raised.exception.code, "EVIDENCE_SHA_MISMATCH")

    def test_supersede_creates_new_candidate_round_and_history(self) -> None:
        first = sidecar.command_start(args_for(self.repo, self.data), self.paths)
        old_sha = first["request"]["candidate_sha"]
        reviewer, _marker = sidecar.reviewer_worktree(self.paths, first["request"])
        roborev_dir = sidecar.roborev_round_dir(self.paths, first["request"])
        roborev_dir.mkdir(parents=True)
        (roborev_dir / "reviews.db").write_bytes(b"old")
        self.assertTrue(reviewer.exists())
        new_sha = self.fixture.commit("feature.txt", "updated\n", "repair")
        result = sidecar.command_supersede(args_for(self.repo, self.data), self.paths)
        state = sidecar.load_json(self.paths.state())
        self.assertEqual(result["request"]["candidate_sha"], new_sha)
        self.assertEqual(state["history"][0]["candidate_sha"], old_sha)
        self.assertEqual(state["history"][0]["status"], "superseded")
        self.assertFalse(reviewer.exists())
        self.assertFalse(roborev_dir.exists())

    def test_run_recovers_missing_delivery_after_gate_persisted(self) -> None:
        started = sidecar.command_start(args_for(self.repo, self.data), self.paths)
        round_dir = pathlib.Path(started["round_dir"])
        gate = {
            "dispatch_id": "dispatch-1",
            "candidate_sha": git(self.repo, "rev-parse", "HEAD"),
            "round": 1,
            "verdict": "approved",
            "findings": [],
        }
        sidecar.atomic_json(round_dir / "gate.json", gate)
        args = argparse.Namespace(ao_session=None, ao_bin="ao")
        result = sidecar.command_run(args, self.paths)
        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["delivery"]["status"], "not_requested")


class GateContractTests(unittest.TestCase):
    def test_structured_review_normalizes_findings_and_fingerprint(self) -> None:
        output = """
```json
{"verdict":"changes_requested","summary":"发现问题","findings":[{"severity":"important","path":"web/src/App.tsx","line":12,"summary":"状态错误","impact":"页面错误","remediation":"修复状态"}]}
```
## Verdict: FAIL
"""
        result = sidecar.extract_structured_review(output)
        self.assertEqual(result["verdict"], "changes_requested")
        self.assertEqual(result["findings"][0]["severity"], "important")
        self.assertRegex(result["findings"][0]["id"], r"^[0-9a-f]{20}$")

    def test_structured_review_rejects_absolute_path(self) -> None:
        output = '```json\n{"verdict":"changes_requested","findings":[{"severity":"important","path":"/tmp/x","summary":"x"}]}\n```'
        with self.assertRaises(sidecar.SidecarError) as raised:
            sidecar.extract_structured_review(output)
        self.assertEqual(raised.exception.code, "INVALID_REVIEW_OUTPUT")

    def test_roborev_fail_cannot_map_to_approved(self) -> None:
        request = {
            "repository": "john-ops-lab/DataLinkRuntime",
            "dispatch_id": "d",
            "worker_session_id": "w",
            "reviewer_model": "k3",
            "base_sha": "a",
            "merge_base_sha": "a",
            "candidate_sha": "b",
            "tree_sha": "c",
            "round": 1,
        }
        roborev = {
            "event": {
                "job_uuid": "j",
                "sha": "b",
                "agent": "claude-code",
                "verdict": "F",
            },
            "database": {
                "job_uuid": "j",
                "review_uuid": "r",
                "git_ref": "b",
                "agent": "claude-code",
                "status": "done",
                "verdict_bool": 0,
                "session_id": "s",
            },
        }
        _, gate = sidecar.build_gate(
            request,
            roborev,
            {"tools": ["Glob", "Grep", "Read"]},
            {"verdict": "approved", "summary": "ok", "findings": []},
        )
        self.assertEqual(gate["verdict"], "changes_requested")

    def test_identity_mismatch_fails_closed(self) -> None:
        request = {
            "repository": "repo",
            "dispatch_id": "d",
            "worker_session_id": "w",
            "reviewer_model": "k3",
            "base_sha": "a",
            "merge_base_sha": "a",
            "candidate_sha": "b",
            "tree_sha": "c",
            "round": 1,
        }
        roborev = {
            "event": {
                "job_uuid": "wrong",
                "sha": "b",
                "agent": "claude-code",
                "verdict": "P",
            },
            "database": {
                "job_uuid": "j",
                "review_uuid": "r",
                "git_ref": "b",
                "agent": "claude-code",
                "status": "done",
                "verdict_bool": 1,
            },
        }
        _, gate = sidecar.build_gate(
            request,
            roborev,
            {},
            {"verdict": "approved", "summary": "ok", "findings": []},
        )
        self.assertEqual(gate["verdict"], "unknown")


class WrapperTests(unittest.TestCase):
    def test_wrapper_discards_robo_rev_permission_widening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            fake = root / "claude"
            fake.write_text(
                """#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
tools = args[args.index('--tools') + 1].split(',')
permission = args[args.index('--permission-mode') + 1]
print(json.dumps({'type':'system','subtype':'init','tools':tools,'permissionMode':permission,'mcp_servers':[],'plugins':[],'skills':[],'slash_commands':[]}))
print(json.dumps({'type':'result','result':'ok'}))
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            audit = root / "audit.json"
            mcp = root / "mcp.json"
            settings = root / "settings.json"
            mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
            settings.write_text("{}", encoding="utf-8")
            environment = {
                "AO_LOCAL_REVIEW_REAL_CLAUDE": str(fake),
                "AO_LOCAL_REVIEW_AUDIT_FILE": str(audit),
                "AO_LOCAL_REVIEW_EMPTY_MCP": str(mcp),
                "AO_LOCAL_REVIEW_EMPTY_SETTINGS": str(settings),
                "AO_LOCAL_REVIEW_BUNDLE_DIR": str(root),
                "AO_LOCAL_REVIEW_MODEL": "k3",
                "AO_LOCAL_REVIEW_PROFILE_VERSION": "3",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=False),
                mock.patch("sys.stdin.buffer.read", return_value=b"prompt"),
            ):
                self.assertEqual(
                    wrapper.main(
                        [
                            "--dangerously-skip-permissions",
                            "--allowedTools",
                            "Edit,Bash",
                        ]
                    ),
                    0,
                )
            receipt = sidecar.load_json(audit)
            self.assertNotIn(
                "--dangerously-skip-permissions", receipt["effective_argv"]
            )
            self.assertEqual(
                receipt["forbidden_incoming"], ["--dangerously-skip-permissions"]
            )
            self.assertIn("Read,Glob,Grep", receipt["effective_argv"])
            summary = sidecar.validate_audit(receipt)
            self.assertEqual(summary["permission_mode"], "dontAsk")

    def test_validate_audit_rejects_extra_tool(self) -> None:
        audit = {
            "effective_argv": [
                "--tools",
                "Read,Glob,Grep",
                "--permission-mode",
                "dontAsk",
                "--safe-mode",
                "--strict-mcp-config",
            ],
            "init_event": {
                "tools": ["Read", "Glob", "Grep", "Bash"],
                "permissionMode": "dontAsk",
                "mcp_servers": [],
                "plugins": [],
                "skills": [],
                "slash_commands": [],
            },
            "exit_code": 0,
        }
        with self.assertRaises(sidecar.SidecarError) as raised:
            sidecar.validate_audit(audit)
        self.assertEqual(raised.exception.code, "CLAUDE_INIT_MISMATCH")


class RoboRevDatabaseTests(unittest.TestCase):
    def test_terminal_failure_requires_a_new_job_for_same_candidate(self) -> None:
        self.assertTrue(sidecar.review_job_needs_enqueue(None))
        self.assertTrue(sidecar.review_job_needs_enqueue({"status": "failed"}))
        self.assertTrue(sidecar.review_job_needs_enqueue({"status": "canceled"}))
        self.assertFalse(sidecar.review_job_needs_enqueue({"status": "queued"}))
        self.assertFalse(sidecar.review_job_needs_enqueue({"status": "done"}))

    def test_query_uses_read_only_persisted_job_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = pathlib.Path(temporary) / "reviews.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE repos(id INTEGER PRIMARY KEY, root_path TEXT, name TEXT);
                CREATE TABLE review_jobs(id INTEGER PRIMARY KEY, repo_id INTEGER, git_ref TEXT, agent TEXT, model TEXT, status TEXT, session_id TEXT, finished_at TEXT, uuid TEXT, worktree_path TEXT);
                CREATE TABLE reviews(id INTEGER PRIMARY KEY, job_id INTEGER, output TEXT, created_at TEXT, uuid TEXT, verdict_bool INTEGER);
                INSERT INTO repos VALUES(1, '/tmp/reviewer', 'repo');
                INSERT INTO review_jobs VALUES(7, 1, 'abc', 'claude-code', 'k3', 'done', 'session', 'now', 'job-uuid', '/tmp/reviewer');
                INSERT INTO reviews VALUES(9, 7, 'review output', 'now', 'review-uuid', 1);
                """
            )
            connection.commit()
            connection.close()
            result = sidecar.query_roborev_database(database, 7)
            self.assertEqual(result["job_uuid"], "job-uuid")
            self.assertEqual(result["review_uuid"], "review-uuid")
            self.assertEqual(result["verdict_bool"], 1)
            self.assertEqual(
                result["output_sha256"], sidecar.sha256_bytes(b"review output")
            )

    def test_event_wait_recovers_completed_job_from_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            database = root / "reviews.db"
            worktree = root / "reviewer"
            worktree.mkdir()
            candidate = "a" * 40
            connection = sqlite3.connect(database)
            connection.executescript(
                f"""
                CREATE TABLE repos(id INTEGER PRIMARY KEY, root_path TEXT, name TEXT);
                CREATE TABLE review_jobs(id INTEGER PRIMARY KEY, repo_id INTEGER, git_ref TEXT, agent TEXT, model TEXT, status TEXT, session_id TEXT, finished_at TEXT, uuid TEXT, worktree_path TEXT);
                CREATE TABLE reviews(id INTEGER PRIMARY KEY, job_id INTEGER, output TEXT, created_at TEXT, uuid TEXT, verdict_bool INTEGER);
                INSERT INTO repos VALUES(1, '{worktree}', 'repo');
                INSERT INTO review_jobs VALUES(7, 1, '{candidate}', 'claude-code', 'k3', 'done', 'session', 'now', 'job-uuid', '{worktree}');
                INSERT INTO reviews VALUES(9, 7, 'review output', 'now', 'review-uuid', 1);
                """
            )
            connection.commit()
            connection.close()
            stream = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                event = sidecar.wait_for_event(
                    stream,
                    7,
                    root / "events.jsonl",
                    2,
                    database,
                    candidate,
                    worktree,
                )
            finally:
                stream.terminate()
                stream.wait(timeout=5)
                if stream.stdout is not None:
                    stream.stdout.close()
                if stream.stderr is not None:
                    stream.stderr.close()
            self.assertTrue(event["recovered_from_database"])
            self.assertEqual(event["job_uuid"], "job-uuid")
            self.assertEqual(event["verdict"], "P")


class DeliveryRecoveryTests(unittest.TestCase):
    def test_pending_delivery_is_persisted_and_retried_without_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            round_dir = root / "round"
            round_dir.mkdir()
            gate = {
                "dispatch_id": "dispatch",
                "candidate_sha": "a" * 40,
                "round": 1,
                "verdict": "changes_requested",
                "findings": [{"id": "finding"}],
            }
            fake_ao = root / "ao"
            fake_ao.write_text(
                "#!/usr/bin/env sh\necho unavailable >&2\nexit 1\n", encoding="utf-8"
            )
            fake_ao.chmod(0o755)
            first = sidecar.deliver(round_dir, gate, "orchestrator-1", str(fake_ao))
            self.assertEqual(first["status"], "pending")
            self.assertEqual(first["attempts"], 1)
            self.assertTrue((round_dir / "delivery.json").exists())

            fake_ao.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
            fake_ao.chmod(0o755)
            second = sidecar.deliver(round_dir, gate, "orchestrator-1", str(fake_ao))
            self.assertEqual(second["status"], "delivered")
            self.assertEqual(second["attempts"], 2)

            replay = sidecar.deliver(round_dir, gate, "orchestrator-1", str(fake_ao))
            self.assertEqual(replay, second)

    def test_delivery_rejects_changed_payload_after_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            round_dir = pathlib.Path(temporary) / "round"
            round_dir.mkdir()
            first_gate = {
                "dispatch_id": "dispatch",
                "candidate_sha": "a" * 40,
                "round": 1,
                "verdict": "approved",
                "findings": [],
            }
            sidecar.deliver(round_dir, first_gate, None, "ao")
            changed_gate = {**first_gate, "verdict": "changes_requested"}
            with self.assertRaises(sidecar.SidecarError) as raised:
                sidecar.deliver(round_dir, changed_gate, None, "ao")
            self.assertEqual(raised.exception.code, "DELIVERY_CONFLICT")

    def test_reconcile_does_not_send_after_three_failed_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            RepositoryFixture(repo)
            data = base / "ao-data"
            paths = sidecar.Paths.resolve(args_for(repo, data))
            started = sidecar.command_start(args_for(repo, data), paths)
            round_dir = pathlib.Path(started["round_dir"])
            gate = {
                "dispatch_id": "dispatch-1",
                "candidate_sha": git(repo, "rev-parse", "HEAD"),
                "round": 1,
                "verdict": "approved",
                "findings": [],
            }
            sidecar.atomic_json(round_dir / "gate.json", gate)
            sidecar.atomic_json(
                round_dir / "delivery.json",
                {
                    "status": "pending",
                    "attempts": 3,
                    "payload_sha256": sidecar.sha256_bytes(
                        sidecar.delivery_message(gate).encode()
                    ),
                },
            )
            marker = base / "sent"
            fake_ao = base / "ao"
            fake_ao.write_text(
                f"#!/usr/bin/env sh\ntouch '{marker}'\n", encoding="utf-8"
            )
            fake_ao.chmod(0o755)
            args = argparse.Namespace(ao_session="orchestrator", ao_bin=str(fake_ao))
            with self.assertRaises(sidecar.SidecarError) as raised:
                sidecar.command_reconcile(args, paths)
            self.assertEqual(raised.exception.code, "DELIVERY_RETRY_EXHAUSTED")
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
