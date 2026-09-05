"""Deployment policy is not a substitute for the real Linux preflight."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dlr.worker import sandbox

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "runtime_deployment_policy", ROOT / "scripts/check-runtime-deployment.py"
)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


@pytest.mark.parametrize("mountpoint", ["/run/dlr-cgroup", "/run/dlr cgroup"])
def test_private_namespace_checks_kernel_ancestor_mount(monkeypatch, mountpoint):
    monkeypatch.setattr(sandbox, "_pid_cgroup", lambda pid: "/")
    escaped = mountpoint.replace(" ", r"\040")
    monkeypatch.setattr(
        sandbox, "_read", lambda path: f"41 22 0:28 /.. {escaped} rw - cgroup2 cgroup rw\n"
    )
    sandbox.validate_private_cgroup_namespace(Path(mountpoint))


@pytest.mark.parametrize(
    ("process_group", "mount_root", "mountpoint", "filesystem"),
    [
        (
            "/system.slice/dlr-host.service/docker-id",
            "/system.slice/dlr-host.service",
            "/run/dlr-cgroup",
            "cgroup2",
        ),
        ("/", "/", "/run/dlr-cgroup", "cgroup2"),
        ("/", "/../..", "/run/dlr-cgroup", "cgroup2"),
        ("/", "/..", "/run/other-cgroup", "cgroup2"),
        ("/", "/..", "/run/dlr-cgroup", "tmpfs"),
    ],
)
def test_private_namespace_rejects_unproven_topology(
    monkeypatch, process_group, mount_root, mountpoint, filesystem
):
    monkeypatch.setattr(sandbox, "_pid_cgroup", lambda pid: process_group)
    monkeypatch.setattr(
        sandbox,
        "_read",
        lambda path: f"41 22 0:28 {mount_root} {mountpoint} rw - {filesystem} cgroup rw\n",
    )
    with pytest.raises(sandbox.SandboxError) as raised:
        sandbox.validate_private_cgroup_namespace(Path("/run/dlr-cgroup"))
    assert raised.value.code == "sandbox_private_cgroup_namespace_required"


@pytest.fixture(scope="module")
def rendered_deployment():
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is needed for the configuration-only audit")
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("DLR_", "COMPOSE_"))
    }
    env.update(
        DLR_RABBITMQ_USER="audit-user",
        DLR_RABBITMQ_PASSWORD="audit-password",
        DLR_ADMIN_TOKEN="audit-admin",
        DLR_WORKER_TOKEN="audit-worker",
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_default_compose_has_one_execution_topology(rendered_deployment):
    POLICY.validate_deployment(rendered_deployment)
    assert (
        rendered_deployment["services"]["worker"]["environment"]["DLR_WORKER_EXECUTION_SLOTS"]
        == "2"
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("cgroup", "host"),
        ("privileged", True),
        ("cap_add", ["SYS_ADMIN"]),
        ("security_opt", ["apparmor=unconfined"]),
        ("user", "1000:1000"),
    ],
)
def test_deployment_rejects_weakened_worker_policy(rendered_deployment, key, value):
    config = copy.deepcopy(rendered_deployment)
    config["services"]["worker"][key] = value
    with pytest.raises(ValueError):
        POLICY.validate_deployment(config)


@pytest.mark.parametrize("source", ["/", "/sys", "/sys/fs/cgroup", "/var/run/docker.sock"])
def test_deployment_rejects_broad_mounts(rendered_deployment, source):
    config = copy.deepcopy(rendered_deployment)
    config["services"]["worker"]["volumes"].append(
        {"type": "bind", "source": source, "target": "/host"}
    )
    with pytest.raises(ValueError, match="Broad host"):
        POLICY.validate_deployment(config)


@pytest.mark.parametrize("setting", sorted(POLICY.REMOVED_SETTINGS))
def test_deployment_rejects_removed_migration_switches(rendered_deployment, setting):
    config = copy.deepcopy(rendered_deployment)
    config["services"]["control"]["environment"][setting] = "true"
    with pytest.raises(ValueError, match="Removed execution"):
        POLICY.validate_deployment(config)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--unit", "ssh.service"],
        ["--unit", "dlr-../bad.service"],
        ["--cpu-quota", "max"],
        ["--memory-max", "infinity"],
        ["--unknown"],
    ],
)
def test_host_preparation_rejects_invalid_input_before_host_access(arguments):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/prepare-sandbox-host.sh"), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not result.stdout
