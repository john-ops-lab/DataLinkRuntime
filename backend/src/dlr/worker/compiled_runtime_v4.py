"""Trusted build inputs and preflight for dependency-free fixed tasks."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from dlr.common.worker_languages_v4 import (
    JAVA_COMPILER,
    JAVA_EXECUTABLE,
    NODE_EXECUTABLE,
    TYPESCRIPT_COMPILER,
    TYPESCRIPT_ROOT,
)
from dlr.runtime import go_runtime, java_runtime, node_harness, typescript_runtime
from dlr.worker import sandbox

_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/home", "TMPDIR": "/tmp"}
_PROBE_SOURCES = {
    "typescript": (
        "export async function handle(context: DLR.Context, input: {value: number}) {"
        " return {value: input.value + 2}; }"
    ),
    "java": (
        "import java.util.Map; public class Adapter {"
        " public Object handle(Context context, Object input) {"
        '   Number value = (Number) ((Map<?, ?>) input).get("value");'
        '   return Map.of("value", value.intValue() + 2);'
        " }}"
    ),
    "go": (
        "package main\nfunc Handle(ctx *Context, input any) (any, error) {"
        " var typed struct { Value int };"
        " if err := DecodeInput(input, &typed); err != nil { return nil, err };"
        ' return map[string]any{"value": typed.Value + 2}, nil }\n'
    ),
}


def sources(language: str, code: str) -> dict[str, str]:
    if language == "java":
        return {"Adapter.java": code, "DlrRuntime.java": java_runtime.SOURCE}
    if language == "typescript":
        return {
            "adapter.mts": code,
            ".dlr-harness.mjs": node_harness.SOURCE,
            "dlr.d.ts": typescript_runtime.DECLARATIONS,
        }
    if language == "go":
        return {"adapter.go": code, "dlr_runtime.go": go_runtime.SOURCE}
    raise ValueError("Worker runtime unavailable")


def command(language: str) -> list[str]:
    if language == "go":
        return [sandbox.GO_LAUNCH]
    return [sys.executable, "-I", "-m", "dlr.worker.compiled_runtime_v4", language]


def _verify_tool(path: str, *, executable: bool) -> None:
    binary = Path(path)
    for item in (binary, *binary.parents):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError("Worker toolchain unavailable")
    if not binary.is_file() or (executable and not os.access(binary, os.X_OK)):
        raise ValueError("Worker toolchain unavailable")


def _run(language: str) -> None:
    # Both phases run within the original Attempt cgroup, time and tmpfs.
    # This entry is restricted to tasks without business Credential bindings.
    workspace = Path.cwd()
    if not workspace.name.startswith("dlr-exec-") or any(
        key.startswith("DLR_SECRET_") for key in os.environ
    ):
        raise ValueError("Worker runtime unavailable")
    if language == "java":
        directory = workspace / "temp" / "classes"
        directory.mkdir(mode=0o700)
        build = [
            JAVA_COMPILER,
            "-J-XX:+UseSerialGC",
            "-J-XX:ActiveProcessorCount=1",
            "-J-Xmx128m",
            "--release",
            "21",
            "-proc:none",
            "-d",
            str(directory),
            "Adapter.java",
            "DlrRuntime.java",
        ]
        runtime = [
            JAVA_EXECUTABLE,
            "-XX:+UseSerialGC",
            "-XX:ActiveProcessorCount=1",
            "-Xmx128m",
            "-cp",
            str(directory),
            "DlrRuntime",
            str(workspace),
        ]
    elif language == "typescript":
        build = [
            NODE_EXECUTABLE,
            "--max-old-space-size=192",
            TYPESCRIPT_COMPILER,
            "--target",
            "ES2022",
            "--module",
            "NodeNext",
            "--moduleResolution",
            "NodeNext",
            "--strict",
            "--noEmitOnError",
            "--skipLibCheck",
            "--types",
            "node",
            "--typeRoots",
            TYPESCRIPT_ROOT + "/@types",
            "adapter.mts",
            "dlr.d.ts",
        ]
        runtime = [
            NODE_EXECUTABLE,
            str(workspace / ".dlr-harness.mjs"),
            str(workspace),
            str(workspace / "adapter.mjs"),
        ]
    else:
        raise ValueError("Worker runtime unavailable")
    subprocess.run(build, env=_ENVIRONMENT, stdin=subprocess.DEVNULL, check=True)
    os.execve(runtime[0], runtime, _ENVIRONMENT)


def run_preflight(
    language: str, config: sandbox.SandboxConfig, *, recovery_root: Path, runtime_root: Path
) -> bool:
    """Exercise the actual toolchain and runtime under the standard envelope."""
    if language == "go":
        return _go_preflight(config, recovery_root=recovery_root, runtime_root=runtime_root)
    attempt = None
    workspace = None
    passed = False
    try:
        if language == "java":
            for binary in (JAVA_COMPILER, JAVA_EXECUTABLE):
                _verify_tool(binary, executable=True)
        elif language == "typescript":
            _verify_tool(NODE_EXECUTABLE, executable=True)
            _verify_tool(TYPESCRIPT_COMPILER, executable=False)
        else:
            return False
        name = sandbox._new_preflight_identity()
        workspace = runtime_root / name
        workspace.mkdir(mode=0o700)
        limits = sandbox.ResourceLimits(
            cpu_cores=config.cpu_cores,
            memory_bytes=config.memory_bytes,
            pids=config.pids,
            tmp_bytes=config.tmp_bytes,
            nofile=config.nofile,
            execution_timeout_seconds=config.execution_timeout_seconds,
        )
        staged = sources(language, _PROBE_SOURCES[language]) | {
            "input.json": '{"value":40}',
            "runtime_config.json": "{}",
            "input_manifest.json": '{"execution_id":1,"files":[]}',
        }
        probe = (
            "import pathlib,subprocess\n"
            "root=pathlib.Path.cwd(); leaf=root/'dlr-exec-1'; leaf.mkdir()\n"
            "for folder in ('input','temp','output'): (leaf/folder).mkdir()\n"
            f"for name, data in {staged!r}.items(): (leaf/name).write_text(data)\n"
            f"subprocess.run({command(language)!r},cwd=leaf,timeout=10,check=True)\n"
            "(root/'output.json').write_bytes((leaf/'output.json').read_bytes())\n"
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
            process = attempt.start([sys.executable, "-I", "-c", probe], stdout=stream)
            if process.wait(timeout=12) != 0:
                return False
        passed = json.loads((workspace / "output.json").read_text()) == {"value": 42}
    except Exception:
        passed = False
    finally:
        if attempt is not None:
            cleanup = attempt.cleanup()
            passed = passed and cleanup.status == "completed" and not cleanup.residue
            if cleanup.status != "completed" or cleanup.residue:
                workspace = None
        elif workspace is not None and (recovery_root / f"sandbox-{workspace.name}.json").exists():
            workspace = None
        if workspace is not None:
            try:
                shutil.rmtree(workspace)
            except OSError:
                passed = False
    return passed


def _go_preflight(
    config: sandbox.SandboxConfig, *, recovery_root: Path, runtime_root: Path
) -> bool:
    """Run both isolated helpers and verify their final output and release."""
    preflight_root = None
    attempt = None
    passed = False
    try:
        _verify_tool(sandbox.GO_TOOL, executable=True)
        preflight_root = runtime_root / sandbox._new_preflight_identity()
        preflight_root.mkdir(mode=0o700)
        workspace = preflight_root / "dlr-exec-1"
        workspace.mkdir(mode=0o700)
        limits = sandbox.ResourceLimits(
            cpu_cores=config.cpu_cores,
            memory_bytes=config.memory_bytes,
            pids=config.pids,
            tmp_bytes=config.tmp_bytes,
            nofile=config.nofile,
            execution_timeout_seconds=config.execution_timeout_seconds,
        )
        for name in ("input", "temp", "output"):
            (workspace / name).mkdir(mode=0o700)
        data = sources("go", _PROBE_SOURCES["go"]) | {
            "input.json": '{"value":40}',
            "runtime_config.json": "{}",
            "input_manifest.json": '{"execution_id":1,"files":[]}',
        }
        for name, value in data.items():
            path = workspace / name
            path.write_text(value, encoding="utf-8")
            path.chmod(0o600)
        deadline = time.monotonic() + min(40, limits.execution_timeout_seconds)
        for phase in ("build", "run"):
            attempt = sandbox.AttemptSandbox(
                config,
                limits,
                execution_id=1,
                attempt_id=1,
                workspace=workspace,
                recovery_root=recovery_root,
                cgroup_name=preflight_root.name,
            )
            process = attempt.start(command("go"), stdout=subprocess.DEVNULL, _go_phase=phase)
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                return False
            if attempt.read_helper_diagnostic() is not None:
                return False
            cleanup = attempt.cleanup()
            if cleanup.status != "completed" or cleanup.residue:
                return False
            attempt = None
        passed = json.loads((workspace / "output.json").read_text()) == {"value": 42}
    except Exception:
        passed = False
    finally:
        if attempt is not None:
            cleanup = attempt.cleanup()
            if cleanup.status != "completed" or cleanup.residue:
                preflight_root = None
        if preflight_root is not None:
            try:
                shutil.rmtree(preflight_root)
            except OSError:
                passed = False
    return passed


if __name__ == "__main__":
    try:
        _run(sys.argv[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        sys.exit(2)
