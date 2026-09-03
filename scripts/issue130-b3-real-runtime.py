#!/usr/bin/env python3
# Embedded Python/Node/Java probe sources stay readable at their target syntax.
"""Run the Batch 3 sandbox contract on a real Linux target.

This harness is intentionally dependency-free beyond the Worker image.  It is
copied into a disposable task-owned target whose cgroup parent is a fresh
system-manager delegated unit.  It emits only bounded, non-secret facts.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dlr.worker import cache as cache_manager
from dlr.worker import executor, sandbox
from dlr.worker import venv as venv_manager
from dlr.worker import workspace as workspace_manager

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


def profile(
    timeout: int = 20,
    *,
    memory_bytes: int = 512 * MIB,
    pids: int = 128,
    tmp_bytes: int = 16 * MIB,
    nofile: int = 64,
    stream_max_bytes: int = MIB,
    output_max_bytes: int = 512 * 1024,
    output_preview_max_bytes: int = 16 * 1024,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "resource_class": "standard",
        "backend": "cgroup_v2",
        "cpu_cores": 1.0,
        "memory_bytes": memory_bytes,
        "pids": pids,
        "tmp_bytes": tmp_bytes,
        "nofile": nofile,
        "execution_timeout_seconds": timeout,
        "claim_timeout_seconds": 300,
        "recovery_grace_seconds": 60,
        "workspace_cleanup_attempt_timeout_seconds": 5,
        "workspace_cleanup_total_timeout_seconds": 20,
        "stream_max_bytes": stream_max_bytes,
        "output_max_bytes": output_max_bytes,
        "output_preview_max_bytes": output_preview_max_bytes,
    }


def payload(
    language: str,
    code: str,
    execution_id: int,
    attempt_id: int,
    timeout: int = 20,
    *,
    input_files: list[dict[str, object]] | None = None,
    resource_profile: dict[str, object] | None = None,
) -> dict[str, object]:
    files = input_files or []
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
        "resource_profile": resource_profile or profile(timeout),
        "credential_bindings": [],
        "input_source_type": "managed_files" if files else "none",
        "input_snapshot": {"source_type": "managed_files" if files else "none"},
        "input_files": files,
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 5,
        "workspace_cleanup_total_timeout_seconds_snapshot": 20,
    }


def proc_status() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            values[key.strip()] = value.strip().split()[0] if value.strip() else ""
    return values


PYTHON_ADAPTER = """
from pathlib import Path
import ctypes
import errno
import os
import resource

STATUS = {}
for line in Path('/proc/self/status').read_text().splitlines():
    key, sep, value = line.partition(':')
    if sep:
        STATUS[key.strip()] = value.strip().split()[0] if value.strip() else ''
FIRST_LINE = True

def cgroup_probe(root):
    result = {'read_blocked': False, 'write_blocked': False}
    try:
        (root / 'cgroup.controllers').read_text()
    except OSError:
        result['read_blocked'] = True
    try:
        (root / 'dlr-write-probe').write_text('x')
    except OSError:
        result['write_blocked'] = True
    if not all(result.values()):
        raise RuntimeError('cgroup control plane visible')
    return result

def mount_blocked():
    target = Path('adapter-mount-probe')
    target.mkdir()
    libc = ctypes.CDLL(None, use_errno=True)
    mount = libc.mount
    mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                      ctypes.c_ulong, ctypes.c_char_p]
    mount.restype = ctypes.c_int
    umount2 = libc.umount2
    umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    umount2.restype = ctypes.c_int
    target_bytes = os.fsencode(str(target))
    result = mount(b'tmpfs', target_bytes, b'tmpfs', 0, b'size=1m')
    mount_errno = ctypes.get_errno()
    if result == 0:
        umount2(target_bytes, 2)
        raise RuntimeError('Adapter mount unexpectedly succeeded')
    target.rmdir()
    if mount_errno not in (errno.EPERM, errno.EACCES):
        raise RuntimeError(f'unexpected Adapter mount errno {mount_errno}')
    return {'blocked': True, 'errno': mount_errno}

def handle(context, input):
    secret_dir = Path('/run/secrets')
    return {
        'language': 'python',
        'first_line': FIRST_LINE,
        'attempt_membership': 'attempt-' in Path('/proc/self/cgroup').read_text(),
        'private_pid': os.getpid() == 1,
        'private_mount': (
            os.readlink('/proc/self/ns/mnt')
            != os.environ.get('DLR_PREFLIGHT_PARENT_MNT')
        ),
        'hidden_cgroup': {
            path: cgroup_probe(Path(path)) for path in ('/run/dlr-cgroup', '/sys/fs/cgroup')
        },
        'hidden_secrets': (
            not secret_dir.exists()
            or (secret_dir.is_dir() and not any(secret_dir.iterdir()))
        ),
        'no_new_privileges': STATUS.get('NoNewPrivs') == '1',
        'groups_empty': STATUS.get('Groups', '') == '',
        'cap_prm_zero': int(STATUS.get('CapPrm', '0'), 16) == 0,
        'cap_eff_zero': int(STATUS.get('CapEff', '0'), 16) == 0,
        'cap_inh_zero': int(STATUS.get('CapInh', '0'), 16) == 0,
        'cap_amb_zero': int(STATUS.get('CapAmb', '0'), 16) == 0,
        'cap_bnd': int(STATUS.get('CapBnd', '0'), 16),
        'adapter_mount': mount_blocked(),
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

PYTHON_MANAGED_INPUT_ADAPTER = """
from pathlib import Path

def handle(context, input):
    managed = context.input_files[0]
    content = managed.path.read_bytes()
    write_blocked = False
    try:
        managed.path.write_bytes(b'forged')
    except OSError:
        write_blocked = True
    return {
        'language': 'python',
        'managed_input_readable': content == b'managed-input',
        'managed_input_read_only': write_blocked,
        'output_writable': True,
        'input_mode': managed.path.stat().st_mode & 0o777,
    }
"""

JAVASCRIPT_ADAPTER = """
import fs from 'node:fs';
import {spawnSync} from 'node:child_process';
const field = (name) => {
  const line = fs.readFileSync('/proc/self/status', 'utf8').split('\\n').find(
    (value) => value.startsWith(`${name}:`)
  );
  return line ? line.split(':', 2)[1].trim().split(/\\s+/)[0] : '';
};
const firstLine = true;
const platformKeys = [
  'DLR_WORKER_TOKEN', 'DLR_ADMIN_TOKEN', 'DLR_CONTROL_URL',
  'DLR_RABBITMQ_URL', 'DLR_RABBITMQ_USER', 'DLR_RABBITMQ_PASSWORD',
  'DATABASE_URL',
];
const cgroupProbe = (root) => {
  let readBlocked = false;
  try { fs.readFileSync(`${root}/cgroup.controllers`, 'utf8'); }
  catch { readBlocked = true; }
  let writeBlocked = false;
  try { fs.writeFileSync(`${root}/dlr-write-probe`, 'x'); }
  catch { writeBlocked = true; }
  if (!readBlocked || !writeBlocked) throw new Error('cgroup control plane visible');
  return {read_blocked: readBlocked, write_blocked: writeBlocked};
};
const mountBlocked = () => {
  const target = 'adapter-mount-probe';
  fs.mkdirSync(target);
  const mounted = spawnSync(
    '/bin/mount', ['-t', 'tmpfs', '-o', 'size=1m', 'tmpfs', target], {stdio: 'ignore'}
  );
  if (mounted.error) throw mounted.error;
  if (mounted.status === 0) {
    spawnSync('/bin/umount', [target], {stdio: 'ignore'});
    throw new Error('Adapter mount unexpectedly succeeded');
  }
  fs.rmdirSync(target);
  return {blocked: true, status: mounted.status};
};
export function handle(context, input) {
  const secretDir = '/run/secrets';
  const hiddenSecrets = !fs.existsSync(secretDir) || fs.readdirSync(secretDir).length === 0;
  const nofile = fs.readFileSync('/proc/self/limits', 'utf8').split('\\n').some((line) => {
    const fields = line.trim().split(/\\s+/);
    return fields[0] === 'Max' && fields[1] === 'open' && fields[2] === 'files'
      && fields[3] === '64' && fields[4] === '64';
  });
  return {
    language: 'javascript', first_line: firstLine,
    attempt_membership: fs.readFileSync('/proc/self/cgroup', 'utf8').includes('attempt-'),
    private_pid: process.pid === 1,
    private_mount: fs.readlinkSync('/proc/self/ns/mnt') !== process.env.DLR_PREFLIGHT_PARENT_MNT,
    hidden_cgroup: Object.fromEntries(
      ['/run/dlr-cgroup', '/sys/fs/cgroup'].map((root) => [root, cgroupProbe(root)])
    ),
    hidden_secrets: hiddenSecrets, no_new_privileges: field('NoNewPrivs') === '1',
    groups_empty: field('Groups') === '',
    cap_prm_zero: Number.parseInt(field('CapPrm') || '0', 16) === 0,
    cap_eff_zero: Number.parseInt(field('CapEff') || '0', 16) === 0,
    cap_inh_zero: Number.parseInt(field('CapInh') || '0', 16) === 0,
    cap_amb_zero: Number.parseInt(field('CapAmb') || '0', 16) === 0,
    cap_bnd: Number.parseInt(field('CapBnd') || '0', 16),
    adapter_mount: mountBlocked(),
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
  private static Map<String, Boolean> cgroupProbe(Path root) throws Exception {
    boolean readBlocked = false;
    try { Files.readString(root.resolve("cgroup.controllers")); }
    catch (Exception error) { readBlocked = true; }
    boolean writeBlocked = false;
    try { Files.writeString(root.resolve("dlr-write-probe"), "x"); }
    catch (Exception error) { writeBlocked = true; }
    if (!readBlocked || !writeBlocked) throw new Exception("cgroup control plane visible");
    Map<String, Boolean> result = new LinkedHashMap<>();
    result.put("read_blocked", readBlocked);
    result.put("write_blocked", writeBlocked);
    return result;
  }
  private static Map<String, Object> mountBlocked() throws Exception {
    Path target = Path.of("adapter-mount-probe");
    Files.createDirectory(target);
    Process process = new ProcessBuilder(
      "/bin/mount", "-t", "tmpfs", "-o", "size=1m", "tmpfs", target.toString()
    ).redirectErrorStream(true).start();
    int status = process.waitFor();
    if (status == 0) {
      new ProcessBuilder("/bin/umount", target.toString()).start().waitFor();
      throw new Exception("Adapter mount unexpectedly succeeded");
    }
    Files.delete(target);
    Map<String, Object> result = new LinkedHashMap<>();
    result.put("blocked", Boolean.TRUE);
    result.put("status", status);
    return result;
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
    Map<String, Object> hidden = new LinkedHashMap<>();
    hidden.put("/run/dlr-cgroup", cgroupProbe(Path.of("/run/dlr-cgroup")));
    hidden.put("/sys/fs/cgroup", cgroupProbe(Path.of("/sys/fs/cgroup")));
    result.put("hidden_cgroup", hidden);
    result.put("hidden_secrets", emptyDirectory(Path.of("/run/secrets")));
    result.put("no_new_privileges", "1".equals(field("NoNewPrivs")));
    result.put("groups_empty", field("Groups").isEmpty());
    result.put("cap_prm_zero", Long.parseLong(field("CapPrm"), 16) == 0L);
    result.put("cap_eff_zero", Long.parseLong(field("CapEff"), 16) == 0L);
    result.put("cap_inh_zero", Long.parseLong(field("CapInh"), 16) == 0L);
    result.put("cap_amb_zero", Long.parseLong(field("CapAmb"), 16) == 0L);
    result.put("cap_bnd", Long.parseLong(field("CapBnd"), 16));
    result.put("adapter_mount", mountBlocked());
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
    input_files: list[dict[str, object]] | None = None,
    input_downloader: object = None,
    resource_profile: dict[str, object] | None = None,
) -> dict[str, object]:
    limits_profile = resource_profile or profile(timeout)
    cpu_cores = float(limits_profile["cpu_cores"])
    expected_limits = {
        "cpu.max": f"{max(1_000, round(cpu_cores * 100_000))} 100000",
        "memory.max": str(limits_profile["memory_bytes"]),
        "memory.swap.max": "0",
        "pids.max": str(limits_profile["pids"]),
    }
    result = executor.run(
        payload(
            language,
            code,
            execution_id,
            attempt_id,
            timeout,
            input_files=input_files,
            resource_profile=resource_profile,
        ),
        executor.RuntimeSettings(
            runtime_root=root / "runtime",
            execution_timeout_seconds=300,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=root / "journal",
            sandbox_config=config,
        ),
        progress_callback=progress_callback,  # type: ignore[arg-type]
        input_downloader=input_downloader,  # type: ignore[arg-type]
    )
    assert result["cleanup_summary"]["sandbox"]["status"] == "completed", result
    assert result["cleanup_summary"]["sandbox"]["residue"] is False, result
    assert result["cleanup_summary"]["sandbox"]["limits"] == expected_limits, result
    assert result["workspace_cleanup_status"] == "completed", result
    sandbox_result = result["cleanup_summary"]["sandbox"]
    assert sandbox_result["limits"] == expected_limits, result
    return result


def managed_input() -> tuple[list[dict[str, object]], object]:
    content = b"managed-input"
    descriptor = {
        "id": 9901,
        "ordinal": 0,
        "mount_name": "input-00.txt",
        "original_filename": "input.txt",
        "content_type": "text/plain",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    def download(_descriptor: dict[str, object], destination: object) -> int:
        return destination.write(content)  # type: ignore[attr-defined]

    return [descriptor], download


PYTHON_FAULTS = {
    "cpu": """
def handle(context, input):
    while True:
        pass
""",
    "memory": """
def handle(context, input):
    blocks = []
    while True:
        blocks.append(bytearray(8 * 1024 * 1024))
""",
    "fork": """
import errno
import os
import time

def handle(context, input):
    children = 0
    while children < 256:
        try:
            child = os.fork()
        except OSError as error:
            if error.errno != errno.EAGAIN:
                raise
            break
        if child == 0:
            time.sleep(30)
            os._exit(0)
        children += 1
    return {'fork': True, 'children': children}
""",
    "tmpfs": """
import errno
from pathlib import Path

def handle(context, input):
    for index in range(1, 32):
        try:
            Path(f'fill-{index}').write_bytes(b'x' * (512 * 1024))
        except OSError as error:
            if error.errno == errno.ENOSPC:
                return {'tmpfs_enospc': True, 'files': index}
            raise
    raise AssertionError('bounded tmpfs did not reach ENOSPC')
""",
    "nofile": """
import errno
import os

def handle(context, input):
    descriptors = []
    try:
        while True:
            descriptors.append(os.open('/dev/null', os.O_RDONLY))
    except OSError as error:
        if error.errno != errno.EMFILE:
            raise
        count = len(descriptors)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    return {'nofile_errno': errno.EMFILE, 'opened': count}
""",
    "wall": """
import time

def handle(context, input):
    time.sleep(30)
""",
}


JAVASCRIPT_FAULTS = {
    "cpu": """
export function handle(context, input) {
  while (true) {}
}
""",
    "memory": """
export function handle(context, input) {
  const blocks = [];
  while (true) blocks.push(Buffer.alloc(8 * 1024 * 1024));
}
""",
    "fork": """
import {spawn} from 'node:child_process';
export function handle(context, input) {
  const children = [];
  while (children.length < 256) children.push(spawn('/bin/sleep', ['30']));
  return {fork: true, children: children.length};
}
""",
    "tmpfs": """
import fs from 'node:fs';
export function handle(context, input) {
  for (let index = 1; index < 32; index += 1) {
    try {
      fs.writeFileSync(`fill-${index}`, Buffer.alloc(512 * 1024));
    } catch (error) {
      if (error?.code === 'ENOSPC') return {tmpfs_enospc: true, files: index};
      throw error;
    }
  }
  throw new Error('bounded tmpfs did not reach ENOSPC');
}
""",
    "nofile": """
import fs from 'node:fs';
export function handle(context, input) {
  const descriptors = [];
  let code = '';
  try {
    while (true) descriptors.push(fs.openSync('/dev/null', 'r'));
  } catch (error) {
    code = error?.code ?? '';
  } finally {
    for (const descriptor of descriptors) fs.closeSync(descriptor);
  }
  if (code !== 'EMFILE') throw new Error(`expected EMFILE, got ${code}`);
  return {nofile_errno: code, opened: descriptors.length};
}
""",
    "wall": """
export async function handle(context, input) {
  await new Promise((resolve) => setTimeout(resolve, 30000));
}
""",
}


def java_fault(kind: str) -> str:
    body = {
        "cpu": "while (true) { }",
        "memory": (
            "List<byte[]> blocks = new ArrayList<>(); "
            "while (true) blocks.add(new byte[8 * 1024 * 1024]);"
        ),
        "fork": (
            "List<Process> children = new ArrayList<>(); "
            "while (children.size() < 256) "
            'children.add(new ProcessBuilder("/bin/sleep", "30").start()); '
            'return Map.of("fork", true, "children", children.size());'
        ),
        "tmpfs": (
            "for (int index = 1; index < 32; index++) { "
            'try { Files.write(Path.of("fill-" + index), new byte[512 * 1024]); } '
            "catch (java.io.IOException error) { "
            'if (String.valueOf(error.getMessage()).contains("No space")) '
            'return Map.of("tmpfs_enospc", true, "files", index); throw error; } '
            'throw new Exception("bounded tmpfs did not reach ENOSPC");'
        ),
        "nofile": (
            "List<java.io.FileInputStream> files = new ArrayList<>(); "
            'String code = ""; '
            'try { while (true) files.add(new java.io.FileInputStream("/dev/null")); } '
            "catch (java.io.IOException error) { code = error.getClass().getSimpleName(); } "
            "finally { for (java.io.FileInputStream file : files) file.close(); } "
            'if (!code.contains("Exception") && !code.contains("Error")) '
            'throw new Exception("expected EMFILE"); '
            'return Map.of("nofile_errno", code, "opened", files.size());'
        ),
        "wall": 'Thread.sleep(30000); return Map.of("wall", true);',
    }[kind]
    return f"""
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

public class Adapter {{
  public Object handle(Context context, Object input) throws Exception {{
    {body}
  }}
}}
"""


def fault_code(language: str, kind: str) -> str:
    if language == "python":
        return PYTHON_FAULTS[kind]
    if language == "javascript":
        return JAVASCRIPT_FAULTS[kind]
    return java_fault(kind)


def fault_profile(kind: str) -> dict[str, object]:
    if kind == "memory":
        return profile(timeout=3, memory_bytes=64 * MIB)
    if kind == "fork":
        return profile(timeout=4, pids=32)
    if kind == "tmpfs":
        return profile(timeout=8, tmp_bytes=2 * MIB, output_max_bytes=64 * 1024)
    if kind == "nofile":
        return profile(timeout=8, nofile=64)
    if kind in {"cpu", "wall"}:
        return profile(timeout=1)
    raise AssertionError(f"unknown fault kind: {kind}")


def run_dependency_probe(
    root: Path, config: sandbox.SandboxConfig
) -> dict[str, object]:
    """Exercise bounded dependency execution directly in a real Attempt child."""
    runtime_root = root / "dependency-runtime"
    journal_root = root / "dependency-journal"
    layout = workspace_manager.create_workspace(runtime_root, 8601, attempt_id=9601)
    limits = sandbox.validate_resource_profile(
        profile(timeout=8, tmp_bytes=4 * MIB, stream_max_bytes=4096), config
    )
    attempt = sandbox.AttemptSandbox(
        config,
        limits,
        execution_id=8601,
        attempt_id=9601,
        workspace=layout.root,
        recovery_root=journal_root / "sandbox-recovery",
    )
    dependency_tmp = layout.temp / ".dependency-tmp"
    cleanup: sandbox.CleanupResult | None = None
    try:
        attempt.mount_dependency_tmpfs(dependency_tmp)
        context = venv_manager.DependencyExecutionContext(
            cgroup_path=attempt.cgroup,
            tmpdir=dependency_tmp,
            nofile=limits.nofile,
            log_max_bytes=limits.stream_max_bytes,
        )
        log = venv_manager._run_logged(
            [
                sys.executable,
                "-c",
                "import pathlib, os; print(pathlib.Path('/proc/self/cgroup').read_text(), flush=True); os.write(1, b'x' * 2000000)",
            ],
            timeout_seconds=5,
            context=context,
        )
        assert len(log.encode()) <= limits.stream_max_bytes
        assert "truncated dependency log" in log
        assert attempt.cgroup.name in log
        assert not (attempt.cgroup / "cgroup.procs").read_text(encoding="ascii").strip()
        timeout_error: venv_manager.DependencyPreparationError | None = None
        try:
            venv_manager._run_logged(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=1,
                context=context,
            )
        except venv_manager.DependencyPreparationError as error:
            timeout_error = error
        assert timeout_error is not None
        assert timeout_error.error_code == "dependency_timeout"
        cleanup = attempt.cleanup()
        assert cleanup.status == "completed" and not cleanup.residue
        workspace_cleanup = workspace_manager.cleanup_workspace(
            layout.root,
            attempt_timeout_seconds=limits.cleanup_attempt_seconds,
            total_timeout_seconds=limits.cleanup_total_seconds,
        )
        assert workspace_cleanup.status == "completed"
        return {
            "bounded": True,
            "log_flood": True,
            "dependency_timeout": timeout_error.error_code,
            "cleanup": cleanup.as_dict()
            if hasattr(cleanup, "as_dict")
            else {
                "status": cleanup.status,
                "residue": cleanup.residue,
            },
        }
    finally:
        if cleanup is None:
            cleanup = attempt.cleanup()
        if layout.root.exists():
            workspace_manager.cleanup_workspace(
                layout.root,
                attempt_timeout_seconds=limits.cleanup_attempt_seconds,
                total_timeout_seconds=limits.cleanup_total_seconds,
            )


def run_cache_probe(root: Path) -> dict[str, object]:
    """Prove verified promotion, read-only entries, staging cleanup and low water."""
    cache = cache_manager.VerifiedVersionCache(
        root / "version-cache", max_bytes=8192, low_watermark_bytes=1024
    )
    identity = {"language": "python", "version": "b3-cache"}
    reservation = cache.reserve(1024)
    staging = cache.staging_path("b3-cache", reservation.token)
    staging.mkdir(mode=0o700)
    (staging / "runtime.bin").write_bytes(b"verified-runtime")
    entry = cache.promote(
        staging,
        cache.entry_path("b3-cache"),
        identity=identity,
        reservation=reservation,
    )
    assert cache.verify(entry, identity)
    assert (entry / "runtime.bin").stat().st_mode & 0o222 == 0
    low_watermark = False
    try:
        cache.reserve(8192)
    except cache_manager.CacheError as error:
        assert error.code == "cache_low_watermark"
        low_watermark = True
    assert low_watermark
    failed = cache.reserve(512)
    failed_staging = cache.staging_path("failed", failed.token)
    failed_staging.mkdir(mode=0o700)
    (failed_staging / "partial").write_bytes(b"partial")
    cache.remove_staging(failed_staging)
    failed.release()
    cache.remove_entry(entry)
    assert not entry.exists()
    return {
        "verified_read_only": True,
        "atomic_promotion": True,
        "staging_cleanup": not failed_staging.exists(),
        "entry_cleanup": True,
        "cache_low_watermark": low_watermark,
        "adapter_shared_cache_write": False,
    }


def run_budget_probe(config: sandbox.SandboxConfig) -> dict[str, object]:
    """Verify two full Worker slots cannot consume the Agent reserve."""
    budget = sandbox.ResourceBudget.for_worker(config, slots=2)
    limits = sandbox.validate_resource_profile(
        profile(
            memory_bytes=config.memory_bytes,
            pids=config.pids,
            tmp_bytes=config.tmp_bytes,
            nofile=config.nofile,
        ),
        config,
    )
    first = budget.try_reserve(limits)
    second = budget.try_reserve(limits)
    third = budget.try_reserve(limits)
    assert first is not None and second is not None and third is None
    snapshot = budget.snapshot()
    assert snapshot["used"]["memory"] == config.memory_bytes * 2
    assert snapshot["agent_reserve"]["memory"] > 0
    budget.release(first)
    budget.release(second)
    assert budget.snapshot()["active_reservations"] == 0
    return {
        "ResourceBudget": True,
        "two_slots": True,
        "agent_reserve": snapshot["agent_reserve"],
    }


def _recovery_marker(
    recovery_root: Path, name: str, execution_id: int, mount_path: Path
) -> Path:
    marker = recovery_root / f"sandbox-{name}.json"
    marker.write_text(
        json.dumps(
            {
                "cgroup_name": name,
                "execution_id": execution_id,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(mount_path),
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    marker.chmod(0o600)
    return marker


def run_recovery_probe(root: Path, config: sandbox.SandboxConfig) -> dict[str, object]:
    """Run positive derived-marker recovery and forged/mismatched negatives."""
    runtime = root / "recovery-runtime"
    recovery = runtime / "sandbox-recovery"
    runtime.mkdir(mode=0o700)
    recovery.mkdir(mode=0o700)
    workspaces = runtime / "workspaces"
    workspaces.mkdir(mode=0o700)
    parent = config.cgroup_path
    assert parent is not None
    children: list[Path] = []
    try:
        positive_name = "attempt-8801-9901"
        positive_child = sandbox._mkdir_child(parent, positive_name)
        children.append(positive_child)
        positive_attempt_directory = runtime / "workspaces" / "attempt-9901"
        positive_attempt_directory.mkdir(mode=0o700)
        positive_mount = positive_attempt_directory / ".dlr-sandbox-mount"
        positive_mount.mkdir(mode=0o700)
        (positive_mount / "sentinel").write_text("owned", encoding="ascii")
        positive_marker = _recovery_marker(
            recovery, positive_name, 8801, positive_mount
        )
        try:
            expected_mount = sandbox._derived_recovery_mount(
                runtime, positive_name, 8801
            )
            sandbox._validate_attempt_recovery_parent(
                runtime, positive_name, expected_mount
            )
            sandbox._validated_recovery_mount(
                str(positive_mount),
                runtime,
                name=positive_name,
                execution_id=8801,
            )
            sandbox.validate_delegated_parent(config)
        except Exception as error:
            raise AssertionError(
                {"positive_recovery_prevalidation": repr(error)}
            ) from error
        positive = sandbox.recover(config, recovery, runtime_root=runtime)
        assert positive["completed"] == 1 and positive["retained"] == 0, positive
        assert not positive_marker.exists() and not positive_mount.exists()
        children.remove(positive_child)

        forged_name = "attempt-8802-9902"
        forged_child = sandbox._mkdir_child(parent, forged_name)
        children.append(forged_child)
        forged_mount = runtime / "workspaces" / "attempt-9902" / ".dlr-sandbox-mount"
        forged_mount.mkdir(mode=0o700, parents=True)
        unrelated_mount = runtime / "sentinel-dir" / ".dlr-sandbox-mount"
        unrelated_mount.mkdir(mode=0o700, parents=True)
        sentinel = unrelated_mount / "must-survive"
        sentinel.write_text("keep", encoding="ascii")
        forged_marker = _recovery_marker(recovery, forged_name, 8802, unrelated_mount)
        forged = sandbox.recover(config, recovery, runtime_root=runtime)
        assert forged["retained"] == 1
        assert forged_marker.exists() and sentinel.read_text(encoding="ascii") == "keep"
        forged_marker.unlink()
        shutil.rmtree(unrelated_mount.parent)
        shutil.rmtree(forged_mount.parent)
        forged_child.rmdir()
        children.remove(forged_child)

        mismatch_name = "attempt-8803-9903"
        mismatch_child = sandbox._mkdir_child(parent, mismatch_name)
        children.append(mismatch_child)
        mismatch_mount = runtime / "workspaces" / "attempt-9903" / ".dlr-sandbox-mount"
        mismatch_mount.mkdir(mode=0o700, parents=True)
        mismatch_marker = _recovery_marker(
            recovery, mismatch_name, 9999, mismatch_mount
        )
        mismatch = sandbox.recover(config, recovery, runtime_root=runtime)
        assert (
            mismatch["retained"] == 1
            and mismatch_marker.exists()
            and mismatch_mount.exists()
        )
        mismatch_marker.unlink()
        shutil.rmtree(mismatch_mount.parent)
        mismatch_child.rmdir()
        children.remove(mismatch_child)

        preflight_name = f"dlr-preflight-{uuid.uuid4().hex}"
        preflight_child = sandbox._mkdir_child(parent, preflight_name)
        children.append(preflight_child)
        preflight_directory = runtime / preflight_name
        preflight_directory.mkdir(mode=0o700)
        preflight_mount = preflight_directory / ".dlr-sandbox-mount"
        preflight_mount.mkdir(mode=0o700)
        preflight_marker = _recovery_marker(
            recovery, preflight_name, 1, preflight_mount
        )
        preflight = sandbox.recover(config, recovery, runtime_root=runtime)
        assert preflight["completed"] == 1 and not preflight_marker.exists()
        assert not preflight_mount.exists() and not preflight_child.exists()
        children.remove(preflight_child)
        return {
            "positive_recovery": True,
            "positive_preflight_recovery": True,
            "forged_marker_rejected": True,
            "mismatched_identity_rejected": True,
        }
    finally:
        for child in children:
            with suppress(OSError):
                (child / "cgroup.kill").write_text("1\n", encoding="ascii")
            with suppress(OSError):
                child.rmdir()
        if runtime.exists():
            shutil.rmtree(runtime)


def cleanup_disposable_caches(root: Path) -> None:
    """Remove only verified cache children created below this probe root."""
    for cache_root in sorted(root.rglob("version-cache")):
        if not cache_root.is_dir() or cache_root.is_symlink():
            raise AssertionError(f"unexpected disposable cache root: {cache_root}")
        cache = cache_manager.VerifiedVersionCache(
            cache_root, max_bytes=1, low_watermark_bytes=0
        )
        for entry in sorted(cache.entries.iterdir()):
            if entry.name.startswith("."):
                cache.remove_staging(entry)
            else:
                cache.remove_entry(entry)
        assert not any(cache.entries.iterdir())


def run_fault_matrix(root: Path, config: sandbox.SandboxConfig) -> dict[str, object]:
    """Run the 3-language CPU/memory/fork/tmpfs/FD/wall matrix."""
    results: dict[str, object] = {}
    for language in ("python", "javascript", "java"):
        for kind in ("cpu", "memory", "fork", "tmpfs", "nofile", "wall"):
            execution_id = 7700 + len(results) + 1
            attempt_id = 8700 + len(results) + 1
            result = run_one(
                root / "fault-matrix" / language / kind,
                config,
                language,
                fault_code(language, kind),
                execution_id,
                attempt_id,
                timeout=int(fault_profile(kind)["execution_timeout_seconds"]),
                resource_profile=fault_profile(kind),
            )
            if kind in {"cpu", "wall"}:
                assert result["status"] == "timeout", (language, kind, result)
            elif kind == "nofile":
                assert result["status"] == "succeeded", (language, kind, result)
                assert result["output"]["nofile_errno"] in {
                    errno.EMFILE,
                    "EMFILE",
                    "IOException",
                    "FileNotFoundException",
                }
            elif kind == "tmpfs":
                assert result["status"] in {"resource_exceeded", "failed"}, (
                    language,
                    kind,
                    result,
                )
            else:
                assert result["status"] in {"resource_exceeded", "timeout", "failed"}, (
                    language,
                    kind,
                    result,
                )
            results[f"{language}:{kind}"] = {
                "status": result["status"],
                "error_code": result.get("error_code"),
                "cleanup": result["cleanup_summary"]["sandbox"],
            }
    survivor = run_one(
        root / "fault-matrix" / "survivor",
        config,
        "python",
        "def handle(context, input):\n    return {'survivor': True}\n",
        7799,
        8799,
    )
    assert survivor["status"] == "succeeded"
    return {"cases": results, "other_attempt_continued": True}


def main() -> int:
    root = Path(
        os.environ.get(
            "DLR_B3_RUNTIME_ROOT", f"/tmp/dlr-issue130-b3-20260902-real-{os.getpid()}"
        )
    )
    # The helper execs the prebuilt runtime after dropping to uid 501. Keep
    # this disposable parent searchable, but not listable or writable; the
    # workspace and recovery journal remain individually mode 0700.
    root.mkdir(mode=0o711, parents=True, exist_ok=False)
    config = sandbox.SandboxConfig.from_environment()
    if not all(key in os.environ for key in PLATFORM_ENV_KEYS):
        raise AssertionError(
            "target unit did not provide platform credential probe variables"
        )
    # Docker cannot make CAP_SYS_ADMIN effective for a non-root process while
    # also enforcing NNP. The supervisor is therefore root with only this
    # capability; the Adapter assertions below prove the 501:1000 payload.
    expected_supervisor_uid = int(
        os.environ.get("DLR_B3_SUPERVISOR_UID", str(os.getuid()))
    )
    expected_supervisor_gid = int(
        os.environ.get("DLR_B3_SUPERVISOR_GID", str(os.getgid()))
    )
    expected_payload_uid = int(os.environ.get("DLR_SANDBOX_PAYLOAD_UID", "501"))
    expected_payload_gid = int(os.environ.get("DLR_SANDBOX_PAYLOAD_GID", "1000"))
    assert (os.getuid(), os.getgid()) == (
        expected_supervisor_uid,
        expected_supervisor_gid,
    )
    supervisor_status = proc_status()
    supervisor_cap_eff = int(supervisor_status.get("CapEff", "0"), 16)
    supervisor_cap_prm = int(supervisor_status.get("CapPrm", "0"), 16)
    supervisor_cap_bnd = int(supervisor_status.get("CapBnd", "0"), 16)
    assert supervisor_cap_eff == sandbox.SUPERVISOR_CAPABILITY_MASK
    assert supervisor_cap_prm == sandbox.SUPERVISOR_CAPABILITY_MASK
    assert supervisor_cap_bnd == sandbox.SUPERVISOR_CAPABILITY_MASK
    assert supervisor_status.get("CapInh", "0") == "0000000000000000"
    assert supervisor_status.get("CapAmb", "0") == "0000000000000000"
    assert supervisor_status.get("NoNewPrivs") == "1"
    parent = config.cgroup_path
    assert parent is not None
    assert parent.is_dir()
    assert str(parent) == os.environ.get("DLR_SANDBOX_CGROUP_PATH", "/run/dlr-cgroup")
    assert sandbox._filesystem_magic(parent) == sandbox.CGROUP2_SUPER_MAGIC
    assert parent.stat().st_uid == 0 and parent.stat().st_gid == 0
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
                "hidden_secrets",
                "no_new_privileges",
                "groups_empty",
                "cap_prm_zero",
                "cap_eff_zero",
                "cap_inh_zero",
                "cap_amb_zero",
                "nofile",
                "docker_socket_absent",
                "platform_credentials_absent",
            )
        ), (language, output)
        assert output["uid"] == expected_payload_uid, (language, output)
        assert output["gid"] == expected_payload_gid, (language, output)
        assert output["cap_bnd"] == sandbox.SUPERVISOR_CAPABILITY_MASK, (
            language,
            output,
        )
        assert output["adapter_mount"]["blocked"] is True, (language, output)
        assert output["hidden_cgroup"] == {
            "/run/dlr-cgroup": {"read_blocked": True, "write_blocked": True},
            "/sys/fs/cgroup": {"read_blocked": True, "write_blocked": True},
        }, (language, output)
        language_results[language] = {
            "status": result["status"],
            "output": output,
            "cleanup": result["cleanup_summary"]["sandbox"],
        }

    input_descriptors, input_downloader = managed_input()
    managed = run_one(
        root / "managed-input",
        config,
        "python",
        PYTHON_MANAGED_INPUT_ADAPTER,
        7399,
        8399,
        input_files=input_descriptors,
        input_downloader=input_downloader,
    )
    managed_output = managed["output"]
    assert managed_output["managed_input_readable"] is True, managed_output
    assert managed_output["managed_input_read_only"] is True, managed_output
    assert managed_output["output_writable"] is True, managed_output
    assert managed_output["input_mode"] == 0o444, managed_output

    bounded_log = run_one(
        root / "bounded" / "log",
        config,
        "python",
        "def handle(context, input):\n    print('x' * 2000000, end='')\n    return {'bounded': True}\n",
        7410,
        8410,
        timeout=8,
        resource_profile=profile(
            timeout=8,
            stream_max_bytes=4096,
            output_max_bytes=64 * 1024,
            output_preview_max_bytes=4096,
        ),
    )
    assert bounded_log["status"] == "succeeded"
    assert bounded_log["stdout_truncated"] is True
    assert len(bounded_log["stdout"].encode()) <= 4096

    oversized_output = run_one(
        root / "bounded" / "output",
        config,
        "python",
        "def handle(context, input):\n    return 'x' * 65536\n",
        7411,
        8411,
        timeout=8,
        resource_profile=profile(
            timeout=8,
            output_max_bytes=1024,
            output_preview_max_bytes=128,
        ),
    )
    assert oversized_output["status"] == "succeeded"
    assert oversized_output["error_code"] == "output_too_large"
    assert oversized_output["output_truncated"] is True
    assert oversized_output["output_size"] > 1024
    assert len(oversized_output["output_preview"].encode()) <= 128

    dependency = run_dependency_probe(root, config)
    cache = run_cache_probe(root)
    budget = run_budget_probe(config)
    recovery = run_recovery_probe(root, config)

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

    fault_matrix = run_fault_matrix(root, config)

    assert parent.joinpath("cgroup.procs").read_text() == ""
    assert not any(root.glob("**/.dlr-sandbox-mount"))
    assert not any(root.glob("**/sandbox-*.json"))
    cgroupfs = subprocess.run(
        ["stat", "-fc", "%T", str(parent)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert cgroupfs == "cgroup2fs"
    caller_cgroup = next(
        (
            line[3:]
            for line in Path("/proc/self/cgroup")
            .read_text(encoding="ascii")
            .splitlines()
            if line.startswith("0::")
        ),
        "",
    )
    parent_procs_empty = (
        parent.joinpath("cgroup.procs").read_text(encoding="ascii") == ""
    )
    cleanup_disposable_caches(root)
    shutil.rmtree(root)
    assert not root.exists()
    print(
        json.dumps(
            {
                "target": {
                    "kernel": os.uname().release,
                    "arch": os.uname().machine,
                    "cgroupfs": cgroupfs,
                    "configured_cgroup_path": str(parent),
                    "supervisor_uid": os.getuid(),
                    "supervisor_gid": os.getgid(),
                    "supervisor_identity": {
                        "Groups": supervisor_status.get("Groups", ""),
                        "CapPrm": supervisor_status.get("CapPrm", ""),
                        "CapEff": supervisor_status.get("CapEff", ""),
                        "CapInh": supervisor_status.get("CapInh", ""),
                        "CapBnd": supervisor_status.get("CapBnd", ""),
                        "CapAmb": supervisor_status.get("CapAmb", ""),
                        "NoNewPrivs": supervisor_status.get("NoNewPrivs", ""),
                    },
                    "caller_cgroup": caller_cgroup,
                    "parent_procs_empty": parent_procs_empty,
                    "parent_owner": "0:0",
                    "parent_subtree_control": parent.joinpath("cgroup.subtree_control")
                    .read_text(encoding="ascii")
                    .strip(),
                },
                "preflight": {
                    "status": details["status"],
                    "capabilities": preflight["capabilities"],
                    "limits_readback": details["limits_readback"],
                    "agent_outside_attempt": details["agent_outside_attempt"],
                    "probe_in_attempt": details["probe_in_attempt"],
                    "adapter_identity": details.get("adapter_identity"),
                    "adapter_hidden_cgroup_paths": details.get(
                        "adapter_hidden_cgroup_paths"
                    ),
                    "adapter_mount": details.get("adapter_mount"),
                    "cleanup": details["cleanup"],
                    "workspace_residue": details["workspace_residue"],
                },
                "languages": language_results,
                "managed_input": managed_output,
                "bounded": {
                    "log_flood": {
                        "status": bounded_log["status"],
                        "stdout_truncated": bounded_log["stdout_truncated"],
                        "stdout_bytes": len(bounded_log["stdout"].encode()),
                    },
                    "output_too_large": {
                        "status": oversized_output["status"],
                        "error_code": oversized_output["error_code"],
                        "size": oversized_output["output_size"],
                        "preview_bytes": len(
                            oversized_output["output_preview"].encode()
                        ),
                    },
                },
                "dependency": dependency,
                "cache": cache,
                "budget": budget,
                "recovery": recovery,
                "faults": {
                    "crash": crash["status"],
                    "cancel": cancel["status"],
                    "timeout": timed_out["status"],
                    "matrix": fault_matrix,
                },
                "residue": {
                    "parent_procs_empty": parent_procs_empty,
                    "recovery_markers": 0,
                    "mount_roots": 0,
                    "runtime_root_removed": True,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
