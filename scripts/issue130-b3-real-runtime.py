#!/usr/bin/env python3
"""Run the Batch 3 sandbox contract on a real Linux target.

This harness is intentionally dependency-free beyond the Worker image.  It is
copied into a disposable task-owned target whose cgroup parent is a fresh
system-manager delegated unit.  It emits only bounded, non-secret facts.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dlr.worker import executor, sandbox

MIB = 1024 * 1024
PLATFORM_ENV_KEYS = (
    "DLR_WORKER_TOKEN",
    "DLR_ADMIN_TOKEN",
    "DLR_CONTROL_URL",
    "DLR_RABBITMQ_URL",
    "DLR_RABBITMQ_USER",
    "DLR_RABBITMQ_PASSWORD",
    "DATABASE_URL",
)


def profile(timeout: int = 20) -> dict[str, object]:
    return {
        "schema_version": 1,
        "resource_class": "standard",
        "backend": "cgroup_v2",
        "cpu_cores": 1.0,
        "memory_bytes": 64 * MIB,
        "pids": 64,
        "tmp_bytes": 16 * MIB,
        "nofile": 64,
        "execution_timeout_seconds": timeout,
        "claim_timeout_seconds": 300,
        "recovery_grace_seconds": 60,
        "workspace_cleanup_attempt_timeout_seconds": 5,
        "workspace_cleanup_total_timeout_seconds": 20,
        "stream_max_bytes": MIB,
        "output_max_bytes": 512 * 1024,
        "output_preview_max_bytes": 16 * 1024,
    }


def payload(
    language: str, code: str, execution_id: int, attempt_id: int, timeout: int = 20
) -> dict[str, object]:
    return {
        "dispatch_backend": "rabbitmq",
        "protocol_version": 3,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "attempt_no": 1,
        "fencing_token": 1,
        "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "lease_seconds": 60,
        "renew_seconds": 15,
        "claim_token": "target-test-claim",
        "cleanup_token": "target-test-cleanup",
        "adapter_id": 9100 + execution_id,
        "version_id": 9200 + execution_id,
        "language": language,
        "code": code,
        "requirements": "",
        "runtime_config": {},
        "input": {"case": "issue130-b3"},
        "latest_version_id": 9200 + execution_id,
        "execution_timeout_seconds": timeout,
        "secrets": {},
        "locale": "en",
        "resource_profile": profile(timeout),
        "credential_bindings": [],
        "input_source_type": "none",
        "input_snapshot": {"source_type": "none"},
        "input_files": [],
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 5,
        "workspace_cleanup_total_timeout_seconds_snapshot": 20,
    }


PYTHON_ADAPTER = """
from pathlib import Path
import os
import resource

STATUS = {}
for line in Path('/proc/self/status').read_text().splitlines():
    key, sep, value = line.partition(':')
    if sep:
        STATUS[key.strip()] = value.strip().split()[0] if value.strip() else ''
FIRST_LINE = True

def handle(context, input):
    secret_dir = Path('/run/secrets')
    return {
        'language': 'python',
        'first_line': FIRST_LINE,
        'attempt_membership': 'attempt-' in Path('/proc/self/cgroup').read_text(),
        'private_pid': os.getpid() == 1,
        'private_mount': os.readlink('/proc/self/ns/mnt') != os.environ.get('DLR_PREFLIGHT_PARENT_MNT'),
        'hidden_cgroup': not Path('/sys/fs/cgroup/cgroup.controllers').exists(),
        'hidden_secrets': not secret_dir.exists() or (secret_dir.is_dir() and not any(secret_dir.iterdir())),
        'no_new_privileges': STATUS.get('NoNewPrivs') == '1',
        'cap_eff_zero': int(STATUS.get('CapEff', '0'), 16) == 0,
        'nofile': resource.getrlimit(resource.RLIMIT_NOFILE) == (64, 64),
        'docker_socket_absent': not Path('/var/run/docker.sock').exists(),
        'platform_credentials_absent': all(key not in os.environ for key in (
            'DLR_WORKER_TOKEN', 'DLR_ADMIN_TOKEN', 'DLR_CONTROL_URL',
            'DLR_RABBITMQ_URL', 'DLR_RABBITMQ_USER', 'DLR_RABBITMQ_PASSWORD',
            'DATABASE_URL',
        )),
        'uid': os.getuid(),
        'gid': os.getgid(),
    }
"""

JAVASCRIPT_ADAPTER = """
import fs from 'node:fs';
const field = (name) => {
  const line = fs.readFileSync('/proc/self/status', 'utf8').split('\\n').find((value) => value.startsWith(`${name}:`));
  return line ? line.split(':', 2)[1].trim().split(/\\s+/)[0] : '';
};
const firstLine = true;
const platformKeys = [
  'DLR_WORKER_TOKEN', 'DLR_ADMIN_TOKEN', 'DLR_CONTROL_URL',
  'DLR_RABBITMQ_URL', 'DLR_RABBITMQ_USER', 'DLR_RABBITMQ_PASSWORD',
  'DATABASE_URL',
];
export function handle(context, input) {
  const secretDir = '/run/secrets';
  const hiddenSecrets = !fs.existsSync(secretDir) || fs.readdirSync(secretDir).length === 0;
  const nofile = fs.readFileSync('/proc/self/limits', 'utf8').split('\\n').some((line) => {
    const fields = line.trim().split(/\\s+/);
    return fields[0] === 'Max' && fields[1] === 'open' && fields[2] === 'files' && fields[3] === '64' && fields[4] === '64';
  });
  return {
    language: 'javascript', first_line: firstLine,
    attempt_membership: fs.readFileSync('/proc/self/cgroup', 'utf8').includes('attempt-'),
    private_pid: process.pid === 1,
    private_mount: fs.readlinkSync('/proc/self/ns/mnt') !== process.env.DLR_PREFLIGHT_PARENT_MNT,
    hidden_cgroup: !fs.existsSync('/sys/fs/cgroup/cgroup.controllers'),
    hidden_secrets: hiddenSecrets, no_new_privileges: field('NoNewPrivs') === '1',
    cap_eff_zero: Number.parseInt(field('CapEff') || '0', 16) === 0,
    nofile, docker_socket_absent: !fs.existsSync('/var/run/docker.sock'),
    platform_credentials_absent: platformKeys.every((key) => process.env[key] === undefined),
    uid: Number(field('Uid').split(',')[0]), gid: Number(field('Gid').split(',')[0]),
  };
}
"""

JAVA_ADAPTER = r"""
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class Adapter {
  private static String field(String name) throws Exception {
    for (String line : Files.readAllLines(Path.of("/proc/self/status"))) {
      String[] parts = line.split(":", 2);
      if (parts.length == 2 && parts[0].trim().equals(name)) {
        String[] values = parts[1].trim().split("\\s+");
        return values.length == 0 ? "" : values[0];
      }
    }
    return "";
  }
  private static boolean emptyDirectory(Path path) throws Exception {
    if (!Files.exists(path)) return true;
    try (var entries = Files.list(path)) { return entries.findAny().isEmpty(); }
  }
  public Object handle(Context context, Object input) throws Exception {
    String cgroup = Files.readString(Path.of("/proc/self/cgroup"));
    String mount = Files.readSymbolicLink(Path.of("/proc/self/ns/mnt")).toString();
    Map<String, Object> result = new LinkedHashMap<>();
    result.put("language", "java");
    result.put("first_line", Boolean.TRUE);
    result.put("attempt_membership", cgroup.contains("attempt-"));
    result.put("private_pid", ProcessHandle.current().pid() == 1);
    result.put("private_mount", !mount.equals(System.getenv("DLR_PREFLIGHT_PARENT_MNT")));
    result.put("hidden_cgroup", !Files.exists(Path.of("/sys/fs/cgroup/cgroup.controllers")));
    result.put("hidden_secrets", emptyDirectory(Path.of("/run/secrets")));
    result.put("no_new_privileges", "1".equals(field("NoNewPrivs")));
    result.put("cap_eff_zero", Long.parseLong(field("CapEff"), 16) == 0L);
    result.put("nofile", Files.readString(Path.of("/proc/self/limits")).lines().anyMatch(line -> {
      String[] values = line.trim().split("\\s+");
      return values.length >= 5 && "Max".equals(values[0]) && "open".equals(values[1]) && "files".equals(values[2]) && "64".equals(values[3]) && "64".equals(values[4]);
    }));
    result.put("docker_socket_absent", !Files.exists(Path.of("/var/run/docker.sock")));
    result.put("platform_credentials_absent", List.of(
      "DLR_WORKER_TOKEN", "DLR_ADMIN_TOKEN", "DLR_CONTROL_URL",
      "DLR_RABBITMQ_URL", "DLR_RABBITMQ_USER", "DLR_RABBITMQ_PASSWORD",
      "DATABASE_URL"
    ).stream().allMatch(key -> System.getenv(key) == null));
    result.put("uid", Integer.parseInt(field("Uid").split(",")[0]));
    result.put("gid", Integer.parseInt(field("Gid").split(",")[0]));
    return result;
  }
}
"""


def run_one(
    root: Path,
    config: sandbox.SandboxConfig,
    language: str,
    code: str,
    execution_id: int,
    attempt_id: int,
    timeout: int = 20,
    progress_callback: object = None,
) -> dict[str, object]:
    result = executor.run(
        payload(language, code, execution_id, attempt_id, timeout),
        executor.RuntimeSettings(
            runtime_root=root / "runtime",
            execution_timeout_seconds=300,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=root / "journal",
            sandbox_config=config,
        ),
        progress_callback=progress_callback,  # type: ignore[arg-type]
    )
    assert result["cleanup_summary"]["sandbox"]["status"] == "completed", result
    assert result["cleanup_summary"]["sandbox"]["residue"] is False, result
    assert result["cleanup_summary"]["sandbox"]["limits"] == {
        "cpu.max": "100000 100000",
        "memory.max": "67108864",
        "memory.swap.max": "0",
        "pids.max": "64",
    }, result
    assert result["workspace_cleanup_status"] == "completed", result
    return result


def main() -> int:
    root = Path(
        os.environ.get(
            "DLR_B3_RUNTIME_ROOT", f"/tmp/dlr-issue130-b3-20260902-real-{os.getpid()}"
        )
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = sandbox.SandboxConfig.from_environment()
    if not all(key in os.environ for key in PLATFORM_ENV_KEYS):
        raise AssertionError(
            "target unit did not provide platform credential probe variables"
        )
    # Docker cannot make CAP_SYS_ADMIN effective for a non-root process while
    # also enforcing NNP. The supervisor is therefore root with only this
    # capability; the Adapter assertions below prove the 501:1000 payload.
    expected_uid = int(os.environ.get("DLR_B3_SUPERVISOR_UID", str(os.getuid())))
    expected_gid = int(os.environ.get("DLR_B3_SUPERVISOR_GID", str(os.getgid())))
    assert (os.getuid(), os.getgid()) == (expected_uid, expected_gid)
    parent = config.cgroup_path
    assert parent is not None
    assert parent.is_dir()
    assert parent.joinpath("cgroup.procs").read_text() == ""

    preflight = sandbox.run_preflight(
        config,
        recovery_root=root / "preflight-recovery",
        runtime_root=root,
    )
    details = preflight["details"]
    assert details["status"] == "passed", details
    assert preflight["capabilities"]["preflight_passed"] is True, preflight
    assert details["agent_outside_attempt"] is True, details
    assert details["probe_in_attempt"] is True, details
    assert details["limits_readback"] == {
        "cpu.max": "100000 100000",
        "memory.max": "67108864",
        "memory.swap.max": "0",
        "pids.max": "64",
    }, details
    assert details["cleanup"]["status"] == "completed", details
    assert details["cleanup"]["residue"] is False, details
    assert details["workspace_residue"] is False, details

    language_results: dict[str, object] = {}
    for offset, (language, code) in enumerate(
        (
            ("python", PYTHON_ADAPTER),
            ("javascript", JAVASCRIPT_ADAPTER),
            ("java", JAVA_ADAPTER),
        ),
        start=1,
    ):
        result = run_one(
            root / language, config, language, code, 7300 + offset, 8300 + offset
        )
        output = result["output"]
        assert all(
            output[key] is True
            for key in (
                "first_line",
                "attempt_membership",
                "private_pid",
                "private_mount",
                "hidden_cgroup",
                "hidden_secrets",
                "no_new_privileges",
                "cap_eff_zero",
                "nofile",
                "docker_socket_absent",
                "platform_credentials_absent",
            )
        ), (language, output)
        assert output["uid"] == expected_uid, (language, output)
        assert output["gid"] == expected_gid, (language, output)
        language_results[language] = {
            "status": result["status"],
            "output": output,
            "cleanup": result["cleanup_summary"]["sandbox"],
        }

    crash = run_one(
        root / "faults",
        config,
        "python",
        "def handle(context, input):\n    raise RuntimeError('b3 crash')\n",
        7401,
        8401,
    )
    assert crash["status"] == "failed", crash
    cancel = run_one(
        root / "faults",
        config,
        "python",
        "import time\ndef handle(context, input):\n    time.sleep(30)\n",
        7402,
        8402,
        progress_callback=lambda _stdout, _stderr: True,
    )
    assert cancel["status"] == "cancelled", cancel
    timed_out = run_one(
        root / "faults",
        config,
        "python",
        "import time\ndef handle(context, input):\n    time.sleep(30)\n",
        7403,
        8403,
        timeout=1,
    )
    assert timed_out["status"] == "timeout", timed_out

    assert parent.joinpath("cgroup.procs").read_text() == ""
    assert not any(root.glob("**/.dlr-sandbox-mount"))
    assert not any(root.glob("**/sandbox-*.json"))
    print(
        json.dumps(
            {
                "target": {
                    "kernel": os.uname().release,
                    "arch": os.uname().machine,
                    "cgroupfs": os.popen(f"stat -fc %T {parent.as_posix()}")
                    .read()
                    .strip(),
                    "supervisor_uid": os.getuid(),
                    "supervisor_gid": os.getgid(),
                    "caller_cgroup": next(
                        (
                            line[3:]
                            for line in Path("/proc/self/cgroup")
                            .read_text()
                            .splitlines()
                            if line.startswith("0::")
                        ),
                        "",
                    ),
                    "parent_procs_empty": True,
                },
                "preflight": {
                    "status": details["status"],
                    "capabilities": preflight["capabilities"],
                    "limits_readback": details["limits_readback"],
                    "agent_outside_attempt": details["agent_outside_attempt"],
                    "probe_in_attempt": details["probe_in_attempt"],
                    "cleanup": details["cleanup"],
                    "workspace_residue": details["workspace_residue"],
                },
                "languages": language_results,
                "faults": {
                    "crash": crash["status"],
                    "cancel": cancel["status"],
                    "timeout": timed_out["status"],
                },
                "residue": {
                    "parent_procs_empty": True,
                    "recovery_markers": 0,
                    "mount_roots": 0,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
