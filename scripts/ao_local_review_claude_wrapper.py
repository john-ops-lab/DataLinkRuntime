#!/usr/bin/env python3
"""Capability-enforcing Claude launcher used by ao-local-review.

RoboRev's arguments are treated as untrusted input.  The wrapper constructs a
new allowlisted argv, captures the actual Claude init event, and atomically
writes an audit receipt without modifying the reviewed checkout.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from typing import Any


FORBIDDEN_TOKENS = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--continue",
    "-c",
    "--resume",
    "-r",
    "--agent",
    "--agents",
    "--plugin-dir",
    "--plugin-url",
    "--chrome",
    "--worktree",
    "-w",
}


def utc_now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def effective_argv(_incoming: Sequence[str]) -> list[str]:
    model = os.environ.get("AO_LOCAL_REVIEW_MODEL", "k3")
    empty_mcp = required_env("AO_LOCAL_REVIEW_EMPTY_MCP")
    empty_settings = required_env("AO_LOCAL_REVIEW_EMPTY_SETTINGS")
    bundle_dir = required_env("AO_LOCAL_REVIEW_BUNDLE_DIR")
    return [
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--effort",
        "max",
        "--tools",
        "Read,Glob,Grep",
        "--permission-mode",
        "dontAsk",
        "--safe-mode",
        "--no-chrome",
        "--disable-slash-commands",
        "--mcp-config",
        empty_mcp,
        "--strict-mcp-config",
        "--settings",
        empty_settings,
        "--add-dir",
        bundle_dir,
        "--no-session-persistence",
    ]


def find_init_event(stdout: bytes) -> dict[str, Any] | None:
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("type") == "system" and value.get("subtype") == "init":
            return value
    return None


def atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv)
    started_at = utc_now()
    real_claude = (
        pathlib.Path(required_env("AO_LOCAL_REVIEW_REAL_CLAUDE")).expanduser().resolve()
    )
    audit_file = (
        pathlib.Path(required_env("AO_LOCAL_REVIEW_AUDIT_FILE")).expanduser().resolve()
    )
    if not real_claude.is_file():
        raise RuntimeError(f"real Claude executable not found: {real_claude}")
    if any(token in FORBIDDEN_TOKENS for token in incoming):
        # Forbidden input is recorded but never forwarded. RoboRev v0.66.0
        # currently passes only --allowedTools; this check catches future
        # attempts to widen permissions without weakening the enforced argv.
        pass
    effective = effective_argv(incoming)
    prompt = sys.stdin.buffer.read()
    stdout_bytes = 0
    init_event: dict[str, Any] | None = None
    with (
        tempfile.TemporaryFile() as prompt_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        prompt_file.write(prompt)
        prompt_file.seek(0)
        process = subprocess.Popen(
            [str(real_claude), *effective],
            stdin=prompt_file,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
        )
        if process.stdout is None:
            raise RuntimeError("Claude process has no stdout")
        try:
            for line in iter(process.stdout.readline, b""):
                stdout_bytes += len(line)
                if init_event is None:
                    init_event = find_init_event(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
        except BrokenPipeError:
            process.terminate()
        finally:
            process.stdout.close()
        return_code = process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read()
    try:
        sys.stderr.buffer.write(stderr)
        sys.stderr.buffer.flush()
    except BrokenPipeError:
        pass
    audit = {
        "schema_version": 1,
        "review_profile_version": int(
            os.environ.get("AO_LOCAL_REVIEW_PROFILE_VERSION", "0")
        ),
        "real_claude": str(real_claude),
        "requested_argv": incoming,
        "effective_argv": effective,
        "init_event": init_event,
        "stdin_bytes": len(prompt),
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": len(stderr),
        "exit_code": return_code,
        "started_at": started_at,
        "completed_at": utc_now(),
    }
    atomic_json(audit_file, audit)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
