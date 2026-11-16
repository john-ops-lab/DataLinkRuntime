"""Trusted fixed Node runtime probe inside the existing task sandbox."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from dlr.common.worker_languages_v4 import NODE_EXECUTABLE
from dlr.runtime import node_harness
from dlr.worker import sandbox


def _trusted_executable() -> None:
    path = Path(NODE_EXECUTABLE)
    for entry in (path, *path.parents):
        info = entry.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError("Node runtime unavailable")
    if not stat.S_ISREG(path.lstat().st_mode) or not os.access(path, os.X_OK):
        raise ValueError("Node runtime unavailable")


def run_preflight(
    config: sandbox.SandboxConfig, *, recovery_root: Path, runtime_root: Path
) -> bool:
    """Run the full ESM harness in the unchanged standard resource envelope.

    The generic isolation probe runs first. This second probe proves that the
    actual Node binary, harness and threads fit the production task limits.
    No credentials, environment-selected binaries or package installer reach it.
    """
    attempt = None
    workspace = None
    passed = False
    try:
        _trusted_executable()
        limits = sandbox.ResourceLimits(
            cpu_cores=config.cpu_cores,
            memory_bytes=config.memory_bytes,
            pids=config.pids,
            tmp_bytes=config.tmp_bytes,
            nofile=config.nofile,
            execution_timeout_seconds=config.execution_timeout_seconds,
        )
        name = sandbox._new_preflight_identity()
        workspace = runtime_root / name
        workspace.mkdir(mode=0o700)
        # The harness requires the canonical execution leaf. A trusted probe
        # wrapper creates it inside this disposable sandbox, then copies only
        # its small verification result to the existing output handoff.
        code = r"""
import fs from 'node:fs';
export async function handle(context, input) {
  await Promise.resolve();
  const status = fs.readFileSync('/proc/self/status', 'utf8');
  return {
    value: input.value + context.config.increment,
    version: process.versions.node,
    uid: process.getuid(), gid: process.getgid(),
    mount: fs.readlinkSync('/proc/self/ns/mnt'),
    pid: fs.readlinkSync('/proc/self/ns/pid'),
    cgroup: fs.readFileSync('/proc/self/cgroup', 'utf8'),
    restricted: /^NoNewPrivs:\s+1$/m.test(status) && /^CapEff:\s+0+$/m.test(status),
    environment: Object.keys(process.env).sort(),
    files: context.inputFiles.length
  };
}
"""
        probe = (
            "import json,pathlib,subprocess\n"
            "root=pathlib.Path.cwd(); leaf=root/'dlr-exec-1'; leaf.mkdir()\n"
            "(leaf/'input').mkdir()\n"
            f"(leaf/'.dlr-harness.mjs').write_text({node_harness.SOURCE!r})\n"
            f"(leaf/'adapter.mjs').write_text({code!r})\n"
            "(leaf/'input.json').write_text('{\"value\":40}')\n"
            "(leaf/'runtime_config.json').write_text('{\"increment\":2}')\n"
            "(leaf/'input_manifest.json').write_text('{\"execution_id\":1,\"files\":[]}')\n"
            f"result=subprocess.run([{NODE_EXECUTABLE!r},str(leaf/'.dlr-harness.mjs'),"
            "str(leaf),str(leaf/'adapter.mjs')],env={'PATH':'/usr/bin:/bin'},"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=True)\n"
            "value=json.loads((leaf/'output.json').read_text())\n"
            "(root/'output.json').write_text(json.dumps(value))\n"
        )
        attempt = sandbox.AttemptSandbox(
            config,
            limits,
            execution_id=1,
            attempt_id=1,
            workspace=workspace,
            recovery_root=recovery_root,
            cgroup_name=name,
        )
        with (workspace / ".probe.log").open("wb") as stream:
            import sys

            process = attempt.start([sys.executable, "-I", "-c", probe], stdout=stream)
            if process.wait(timeout=10) != 0:
                return False
        value = json.loads((workspace / "output.json").read_text())
        passed = (
            value["value"] == 42
            and value["version"].split(".")[0] == "22"
            and value["uid"] == config.payload_uid
            and value["gid"] == config.payload_gid
            and value["mount"] != os.readlink("/proc/self/ns/mnt")
            and value["pid"] != os.readlink("/proc/self/ns/pid")
            and name in value["cgroup"]
            and value["restricted"] is True
            and value["environment"] == ["PATH"]
            and value["files"] == 0
            and attempt.limits_readback
            == {
                "cpu.max": sandbox._cpu_max(limits.cpu_cores),
                "memory.max": str(limits.memory_bytes),
                "memory.swap.max": "0",
                "pids.max": str(limits.pids),
            }
        )
    except Exception:  # noqa: BLE001 - never expose probe or environment content
        passed = False
    finally:
        if attempt is not None:
            cleanup = attempt.cleanup()
            passed = passed and cleanup.status == "completed" and not cleanup.residue
            if cleanup.status != "completed" or cleanup.residue:
                workspace = None  # Preserve the authenticated recovery marker.
        elif workspace is not None and (recovery_root / f"sandbox-{workspace.name}.json").exists():
            workspace = None  # Constructor cleanup could not prove the cgroup empty.
        if workspace is not None:
            try:
                shutil.rmtree(workspace)
            except OSError:
                passed = False
    return passed
