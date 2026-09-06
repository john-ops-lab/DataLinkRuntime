#!/usr/bin/env python3
"""Destructive lifecycle regression for a task-owned Compose smoke only.

Run with the same Docker context, env file and Compose files as the smoke.
The script restores the original service configuration and removes only its
temporary peer container/volumes. The enclosing smoke owns its database.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = (ROOT / "scripts/issue144-runtime-api.py").read_text()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--project", required=True)
parser.add_argument("--env-file")
parser.add_argument("-f", "--file", action="append", default=[])
args = parser.parse_args()
if not re.fullmatch(
    r"dlr-(?:(?:compose-)?smoke-[a-zA-Z0-9_-]+|issue144(?:-[a-zA-Z0-9_-]+)?)",
    args.project,
):
    parser.error(
        "use a dedicated dlr-smoke-*, dlr-compose-smoke-* or dlr-issue144 project"
    )


def run(command, *, capture=False, input=None, timeout=180):
    result = subprocess.run(
        command,
        input=input,
        text=True,
        check=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


base = ["docker", "compose", "-p", args.project]
if args.env_file:
    base += ["--env-file", args.env_file]
for file in args.file or [str(ROOT / "docker-compose.yml")]:
    base += ["-f", file]


def compose(*command, config=None, capture=False, input=None, timeout=180):
    prefix = (
        base
        if config is None
        else ["docker", "compose", "-p", args.project, "-f", str(config)]
    )
    return run(prefix + list(command), capture=capture, input=input, timeout=timeout)


def api(phase, config=None):
    compose(
        "exec",
        "-T",
        "-e",
        "ISSUE144_PHASE=" + phase,
        "control",
        "python",
        "-",
        config=config,
        input=API,
        timeout=300,
    )


SNAPSHOT = """
import json,os
from pathlib import Path
root=Path('/run/dlr-cgroup')
mounts=[line for line in Path('/proc/self/mountinfo').read_text().splitlines()
        if ' - cgroup2 ' in line]
group=Path('/proc/self/cgroup').read_text().strip()
assert group=='0::/agent',group
assert root.samefile('/sys/fs/cgroup')
assert not (root/'cgroup.procs').read_text().strip()
assert 'agent' in [p.name for p in root.iterdir() if p.is_dir()]
if os.environ.get('DLR_REQUIRE_NSDELEGATE')=='1':
    assert any('nsdelegate' in line.split(' - ')[1].split()[-1].split(',') for line in mounts)
limit_names=['cpu.max','memory.max','memory.swap.max','pids.max']
result={'membership':group,'root_device':root.stat().st_dev,'root_inode':root.stat().st_ino,
        'limits':{name:(root/name).read_text().strip() for name in limit_names},
        'children':sorted(p.name for p in root.iterdir() if p.is_dir()),'mountinfo':mounts,
        'recovery_markers':len(list((Path(os.environ['DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT'])/'sandbox-recovery').glob('sandbox-*.json')))}
print(json.dumps(result))
"""


def snapshot(service="worker", config=None):
    raw = compose(
        "exec",
        "-T",
        "-e",
        "DLR_REQUIRE_NSDELEGATE=" + os.environ.get("DLR_REQUIRE_NSDELEGATE", "0"),
        service,
        "python",
        "-c",
        SNAPSHOT,
        config=config,
        capture=True,
    )
    result = json.loads(raw)
    print("issue144-kernel=" + json.dumps(result, sort_keys=True), flush=True)
    return result


def assert_clean(config=None, service="worker"):
    result = snapshot(service, config)
    assert result["children"] == ["agent"] and result["recovery_markers"] == 0, result


original = json.loads(compose("config", "--format", "json", capture=True))
container_id = compose("ps", "-q", "worker", capture=True)
assert container_id
actual_project = run(
    [
        "docker",
        "inspect",
        "--format",
        '{{index .Config.Labels "com.docker.compose.project"}}',
        container_id,
    ],
    capture=True,
)
assert actual_project == args.project
worker_image = run(
    ["docker", "inspect", "--format", "{{.Config.Image}}", container_id], capture=True
)
original["services"]["worker"]["image"] = worker_image
peer_volumes = []
with tempfile.TemporaryDirectory(prefix="dlr-issue144-") as directory:
    folder = Path(directory)
    restored = folder / "original.json"
    lifecycle = folder / "lifecycle.json"
    dual_path = folder / "dual.json"

    def save(path, value):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream)

    save(restored, original)
    changed = copy.deepcopy(original)
    changed["services"]["control"]["environment"].update(
        {"DLR_ATTEMPT_LEASE_SECONDS": "30", "DLR_ATTEMPT_RENEW_SECONDS": "5"}
    )
    save(lifecycle, changed)
    dual = copy.deepcopy(changed)
    for service in ("worker", "control"):
        dual["services"][service]["environment"].update(
            {
                "DLR_SANDBOX_CPU_CORES": "0.5",
                "DLR_SANDBOX_MEMORY_BYTES": "268435456",
                "DLR_SANDBOX_PIDS": "64",
                "DLR_WORKER_EXECUTION_SLOTS": "1",
            }
        )
    main = dual["services"]["worker"]
    main.update(
        cpus=0.75, mem_limit=1342177280, memswap_limit=1342177280, pids_limit=256
    )
    peer = copy.deepcopy(main)
    peer["environment"]["DLR_WORKER_NAME"] += "-peer"
    for volume in peer["volumes"]:
        if volume["target"] in {
            "/var/lib/dlr/runtime",
            "/var/lib/dlr/journal",
            "/var/lib/dlr/platform-logs/worker",
        }:
            name = (
                "issue144_peer_"
                + {
                    "/var/lib/dlr/runtime": "runtime",
                    "/var/lib/dlr/journal": "journal",
                    "/var/lib/dlr/platform-logs/worker": "logs",
                }[volume["target"]]
            )
            volume.clear()
            target = {
                "runtime": "/var/lib/dlr/runtime",
                "journal": "/var/lib/dlr/journal",
                "logs": "/var/lib/dlr/platform-logs/worker",
            }[name.rsplit("_", 1)[1]]
            volume.update(type="volume", source=name, target=target)
            full_name = args.project + "_" + name
            dual.setdefault("volumes", {})[name] = {"name": full_name}
            peer_volumes.append(full_name)
    dual["services"]["worker-peer"] = peer
    save(dual_path, dual)
    try:
        api("basic")
        assert_clean()
        compose("restart", "worker", timeout=60)
        compose("up", "-d", "--no-build", "--wait", "worker")
        api("after-restart")
        assert_clean()
        compose(
            "up", "-d", "--no-build", "--wait", "control", "worker", config=lifecycle
        )
        api("start-crash", lifecycle)
        before = snapshot(config=lifecycle)
        assert before["recovery_markers"] == 1 and len(before["children"]) == 2
        compose("kill", "-s", "SIGKILL", "worker", config=lifecycle)
        compose("up", "-d", "--no-build", "--wait", "worker", config=lifecycle)
        api("after-crash", lifecycle)
        assert_clean(lifecycle)
        compose("restart", "worker", config=lifecycle, timeout=60)
        compose("up", "-d", "--no-build", "--wait", "worker", config=lifecycle)
        api("after-restart", lifecycle)
        assert_clean(lifecycle)
        compose("stop", "-t", "20", "worker", config=lifecycle, timeout=45)
        compose(
            "up",
            "-d",
            "--no-build",
            "--wait",
            "control",
            "worker",
            "worker-peer",
            config=dual_path,
        )
        first = snapshot(config=dual_path)
        second = snapshot("worker-peer", dual_path)
        assert first["root_inode"] != second["root_inode"]
        assert (
            first["limits"]["cpu.max"] == second["limits"]["cpu.max"] == "75000 100000"
        )
        api("start-pair", dual_path)
        snapshot(config=dual_path)
        snapshot("worker-peer", dual_path)
        compose("stop", "-t", "20", "worker", config=dual_path, timeout=45)
        compose("rm", "-f", "worker", config=dual_path)
        api("peer-survives", dual_path)
        assert_clean(dual_path, "worker-peer")
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors = []

        def cleanup_action(callback):
            try:
                callback()
            except (
                OSError,
                subprocess.SubprocessError,
                AssertionError,
                ValueError,
            ) as error:
                cleanup_errors.append(error)
                print(
                    "issue144-cleanup=FAILED " + type(error).__name__, file=sys.stderr
                )

        # Exact peer names only; cleanup failures never replace the primary error.
        cleanup_action(
            lambda: compose(
                "stop", "-t", "20", "worker-peer", config=dual_path, timeout=45
            )
        )
        cleanup_action(lambda: compose("rm", "-f", "worker-peer", config=dual_path))

        def remove_peer_volume(name):
            found = run(
                [
                    "docker",
                    "volume",
                    "ls",
                    "--filter",
                    "name=^" + name + "$",
                    "--format",
                    "{{.Name}}",
                ],
                capture=True,
            )
            if found:
                owner = run(
                    [
                        "docker",
                        "volume",
                        "inspect",
                        "--format",
                        '{{index .Labels "com.docker.compose.project"}}',
                        name,
                    ],
                    capture=True,
                )
                assert owner == args.project
                run(["docker", "volume", "rm", name])

        for name in peer_volumes:
            cleanup_action(lambda name=name: remove_peer_volume(name))
        cleanup_action(
            lambda: compose(
                "up", "-d", "--no-build", "--wait", "control", "worker", config=restored
            )
        )
        if cleanup_errors and primary_error is None:
            raise cleanup_errors[0]

print(
    "issue144-lifecycle=PASS normal-restart crash-recovery idempotence dual-worker-isolation",
    flush=True,
)
