import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class ProcessLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.env = dict(os.environ, DLR_PREVIEW_HOME=str(self.root))
        self.env["PYTHONPATH"] = str(TOOLS)

    def write_config(self, *, enabled):
        value = {
            "repo": "owner/repo",
            "pr": 2,
            "enabled": enabled,
            "poll_seconds": 60,
            "profile": "example-vm",
            "project": "example-preview",
            "vm_root": "/example/preview",
            "web_port": 12345,
            "launchagent_label": "org.example.preview",
            "sandbox_unit": "example-preview.service",
            "sandbox_cpu_quota": "100%",
            "sandbox_memory_max": "1G",
        }
        (self.root / "config.json").write_text(json.dumps(value))
        (self.root / "state.json").write_text(json.dumps({"sha": "a" * 40}))

    def holder(self, name):
        code = """
import fcntl, pathlib, sys
with pathlib.Path(sys.argv[1]).open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen(
            [PYTHON, "-c", code, str(self.root / name)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.assertEqual(process.stdout.readline().strip(), "locked")
        self.addCleanup(self.close_process, process)
        return process

    def release(self, process):
        if process.poll() is None:
            process.stdin.write("release\n")
            process.stdin.flush()
            process.wait(timeout=5)

    def close_process(self, process):
        self.release(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def try_lock(self, name):
        code = """
import fcntl, pathlib, sys
with pathlib.Path(sys.argv[1]).open('a') as lock:
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit(75)
"""
        return subprocess.run(
            [PYTHON, "-c", code, str(self.root / name)], env=self.env, check=False
        ).returncode

    def test_singleton_and_operation_are_separate_process_locks(self):
        singleton = self.holder("controller.lock")
        self.assertEqual(self.try_lock("operation.lock"), 0)
        operation = self.holder("operation.lock")
        self.assertEqual(self.try_lock("operation.lock"), 75)
        self.assertEqual(self.try_lock("controller.lock"), 75)
        self.release(operation)
        self.release(singleton)

    def test_tick_holds_operation_until_complete(self):
        self.write_config(enabled=False)
        code = """
import preview, sys
def inner():
    print('tick-entered', flush=True)
    sys.stdin.readline()
    return 'done'
preview._tick = inner
assert preview.tick() == 'done'
"""
        process = subprocess.Popen(
            [PYTHON, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(self.close_process, process)
        self.assertEqual(process.stdout.readline().strip(), "tick-entered")
        self.assertEqual(self.try_lock("operation.lock"), 75)
        self.release(process)
        self.assertEqual(self.try_lock("operation.lock"), 0)

    def test_paused_plan_ignores_live_singleton_and_holds_both_locks(self):
        self.write_config(enabled=False)
        singleton = self.holder("controller.lock")
        code = """
import preview, sys
def plan(*args):
    print('plan-entered', flush=True)
    sys.stdin.readline()
    return {'result': 'ok'}
preview.plan_carry_forward = plan
sys.argv = ['preview.py', 'plan-carry-forward', '--to-sha', 'b'*40,
            '--ids-file', '/private/ids.json', '--output', '/private/manifest.json']
preview.main()
"""
        process = subprocess.Popen(
            [PYTHON, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(self.close_process, process)
        self.assertEqual(process.stdout.readline().strip(), "plan-entered")
        self.assertEqual(self.try_lock("operation.lock"), 75)
        self.assertEqual(self.try_lock("config.lock"), 75)
        self.release(process)
        self.assertEqual(process.returncode, 0, process.stderr.read())
        self.release(singleton)

    def test_enabled_plan_fails_before_plan_side_effect(self):
        self.write_config(enabled=True)
        code = """
import preview, sys
def plan(*args):
    print('PLAN_CALLED')
    return {}
preview.plan_carry_forward = plan
sys.argv = ['preview.py', 'plan-carry-forward', '--to-sha', 'b'*40,
            '--ids-file', '/private/ids.json', '--output', '/private/manifest.json']
preview.main()
"""
        result = subprocess.run(
            [PYTHON, "-c", code],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("PLAN_CALLED", result.stdout)
        self.assertEqual(self.try_lock("operation.lock"), 0)
        self.assertEqual(self.try_lock("config.lock"), 0)

    def test_plan_exception_releases_operation_and_config(self):
        self.write_config(enabled=False)
        code = """
import preview, sys
def plan(*args): raise RuntimeError('planned failure')
preview.plan_carry_forward = plan
sys.argv = ['preview.py', 'plan-carry-forward', '--to-sha', 'b'*40,
            '--ids-file', '/private/ids.json', '--output', '/private/manifest.json']
preview.main()
"""
        result = subprocess.run(
            [PYTHON, "-c", code],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.try_lock("operation.lock"), 0)
        self.assertEqual(self.try_lock("config.lock"), 0)

    def test_busy_installer_pauses_without_bootout_or_copy(self):
        self.write_config(enabled=True)
        operation = self.holder("operation.lock")
        code = """
import install, sys
def side_effect(*args, **kwargs):
    print('EXTERNAL_SIDE_EFFECT')
    raise AssertionError('external action reached')
install.subprocess.run = side_effect
sys.argv = ['install.py']
install.main()
"""
        result = subprocess.run(
            [PYTHON, "-c", code],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("EXTERNAL_SIDE_EFFECT", result.stdout)
        self.assertFalse(json.loads((self.root / "config.json").read_text())["enabled"])
        self.release(operation)

    def test_installer_requires_singleton_after_bootout_before_copy(self):
        self.write_config(enabled=True)
        singleton = self.holder("controller.lock")
        code = """
import install, subprocess, sys
def command(*args, **kwargs):
    print('BOOTOUT_CALLED')
    return subprocess.CompletedProcess(args, 0)
def copy(*args, **kwargs): print('COPY_CALLED')
install.subprocess.run = command
install.shutil.copy2 = copy
sys.argv = ['install.py']
install.main()
"""
        result = subprocess.run(
            [PYTHON, "-c", code],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("BOOTOUT_CALLED", result.stdout)
        self.assertNotIn("COPY_CALLED", result.stdout)
        self.assertEqual(self.try_lock("operation.lock"), 0)
        self.assertEqual(self.try_lock("config.lock"), 0)
        self.release(singleton)

    def test_installer_rejects_config_changed_after_its_pause(self):
        self.write_config(enabled=True)
        code = """
import contextlib, install, sys
original = install.preview.operation_lock
@contextlib.contextmanager
def gated(*, blocking=True):
    print('installer-paused', flush=True)
    sys.stdin.readline()
    with original(blocking=blocking):
        yield
def side_effect(*args, **kwargs):
    print('EXTERNAL_SIDE_EFFECT')
    raise AssertionError('external action reached')
install.preview.operation_lock = gated
install.subprocess.run = side_effect
sys.argv = ['install.py']
install.main()
"""
        process = subprocess.Popen(
            [PYTHON, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.addCleanup(self.close_process, process)
        self.assertEqual(process.stdout.readline().strip(), "installer-paused")
        value = json.loads((self.root / "config.json").read_text())
        value.update(enabled=True, pr=3)
        temporary = self.root / "config.changed"
        temporary.write_text(json.dumps(value))
        os.replace(temporary, self.root / "config.json")
        self.release(process)
        self.assertEqual(process.returncode, 2, process.stderr.read())
        self.assertNotIn("EXTERNAL_SIDE_EFFECT", process.stdout.read())
        self.assertEqual(json.loads((self.root / "config.json").read_text())["pr"], 3)
        self.assertEqual(self.try_lock("operation.lock"), 0)
        self.assertEqual(self.try_lock("config.lock"), 0)

    def test_successful_installer_restores_config_after_receipt(self):
        self.write_config(enabled=True)
        fake_home = self.root / "home"
        plist = fake_home / "Library/LaunchAgents/org.example.preview.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text(
            "<?xml version='1.0' encoding='UTF-8'?>"
            "<!DOCTYPE plist PUBLIC '-//Apple//DTD PLIST 1.0//EN' "
            "'http://www.apple.com/DTDs/PropertyList-1.0.dtd'>"
            "<plist version='1.0'><dict/></plist>"
        )
        code = """
import install, pathlib, subprocess, sys
from types import SimpleNamespace
from unittest.mock import patch
home = pathlib.Path(sys.argv[1])
def command(*args, **kwargs): return subprocess.CompletedProcess(args, 0)
sys.argv = ['install.py']
with patch.object(install.Path, 'home', return_value=home), \
     patch.object(install.subprocess, 'run', side_effect=command), \
     patch.object(install.preview, 'vm_command', return_value=SimpleNamespace()), \
     patch.object(install.preview, 'phase', return_value=True):
    install.main()
"""
        result = subprocess.run(
            [PYTHON, "-c", code, str(fake_home)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads((self.root / "config.json").read_text())["enabled"])
        receipt = json.loads((self.root / "installation.json").read_text())
        self.assertIn("preview.py", receipt["files"])
        self.assertEqual(self.try_lock("operation.lock"), 0)
        self.assertEqual(self.try_lock("config.lock"), 0)
        self.assertEqual(self.try_lock("controller.lock"), 0)


if __name__ == "__main__":
    unittest.main()
