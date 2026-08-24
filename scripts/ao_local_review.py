#!/usr/bin/env python3
"""Local external review sidecar for AO LOCAL_FAST candidates.

The sidecar deliberately stores its state in Git metadata rather than in the
worker checkout.  RoboRev is used only for queueing, Claude invocation, raw
result persistence, and completion events; this module owns candidate identity,
the enforced review profile, the structured gate, delivery recovery, and
archival.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import selectors
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Iterator, Sequence
from typing import Any, NoReturn


SCHEMA_VERSION = 1
REVIEW_PROFILE_VERSION = 3
ROBOREV_VERSION = "0.66.0"
REVIEWER = "claude-code"
DEFAULT_MODEL = "k3"
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_DELIVERY_ATTEMPTS = 3
MACHINE_FIELDS = {"approved", "changes_requested", "unknown"}
BLOCKING_SEVERITIES = {"critical", "important"}


class SidecarError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def utc_now() -> str:
    return (
        dt.datetime.now(dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    command: Sequence[str],
    *,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise SidecarError(
            "COMMAND_FAILED",
            f"command failed ({completed.returncode}): {' '.join(command)}",
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
        )
    return completed


def git(worktree: pathlib.Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(worktree), *args], check=check).stdout.strip()


def ensure_directory(path: pathlib.Path, mode: int = 0o700) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(PermissionError):
        path.chmod(mode)
    return path


def atomic_json(path: pathlib.Path, payload: Any) -> None:
    ensure_directory(path.parent)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SidecarError("STATE_NOT_FOUND", f"missing state file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SidecarError(
            "INVALID_STATE", f"invalid JSON state: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise SidecarError("INVALID_STATE", f"JSON state must be an object: {path}")
    return value


@contextlib.contextmanager
def directory_lock(root: pathlib.Path) -> Iterator[None]:
    ensure_directory(root)
    lock_path = root / ".lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def safe_segment(value: str, field: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise SidecarError("INVALID_ARGUMENT", f"invalid {field}: {value!r}")
    return value


def normalize_repository(remote: str, fallback: str) -> str:
    remote = remote.strip()
    if not remote:
        return fallback
    if remote.startswith("git@") and ":" in remote:
        path = remote.split(":", 1)[1]
    else:
        parsed = urllib.parse.urlparse(remote)
        path = parsed.path if parsed.scheme else remote
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    pieces = [piece for piece in path.split("/") if piece]
    return "/".join(pieces[-2:]) if len(pieces) >= 2 else fallback


def changed_files(worktree: pathlib.Path, range_ref: str) -> list[dict[str, str]]:
    output = git(worktree, "diff", "--name-status", "-z", "--find-renames", range_ref)
    if not output:
        return []
    tokens = output.split("\0")
    result: list[dict[str, str]] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index]
        index += 1
        if status.startswith(("R", "C")):
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            result.append({"status": status, "path": new_path, "old_path": old_path})
        else:
            result.append({"status": status, "path": tokens[index]})
            index += 1
    return result


def evidence_record(path_value: str, candidate_sha: str, kind: str) -> dict[str, Any]:
    path = pathlib.Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise SidecarError(
            "EVIDENCE_NOT_FOUND", f"{kind} evidence is not a file: {path}"
        )
    record: dict[str, Any] = {
        "kind": kind,
        "path": str(path),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }
    if path.suffix.lower() == ".json":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SidecarError(
                "INVALID_EVIDENCE", f"invalid JSON evidence: {path}: {exc}"
            ) from exc
        if isinstance(document, dict):
            evidence_sha = document.get("candidate_sha")
            if evidence_sha is not None and evidence_sha != candidate_sha:
                raise SidecarError(
                    "EVIDENCE_SHA_MISMATCH",
                    f"evidence candidate_sha does not match: {path}",
                    expected=candidate_sha,
                    actual=evidence_sha,
                )
            record["candidate_sha"] = evidence_sha
    return record


@dataclasses.dataclass(frozen=True)
class Paths:
    repo: pathlib.Path
    worker: pathlib.Path
    state_root: pathlib.Path
    data_root: pathlib.Path

    @classmethod
    def resolve(cls, args: argparse.Namespace) -> "Paths":
        worker = (
            pathlib.Path(args.worker_worktree or args.repo or ".")
            .expanduser()
            .resolve()
        )
        repo = pathlib.Path(args.repo or worker).expanduser().resolve()
        if not (worker / ".git").exists():
            raise SidecarError("NOT_GIT_WORKTREE", f"not a Git worktree: {worker}")
        top = pathlib.Path(git(worker, "rev-parse", "--show-toplevel")).resolve()
        if top != worker:
            worker = top
        main_common = pathlib.Path(git(worker, "rev-parse", "--git-common-dir"))
        if not main_common.is_absolute():
            main_common = (worker / main_common).resolve()
        repo = main_common.parent.resolve()
        state_value = git(worker, "rev-parse", "--git-path", "ao-local-review")
        state_root = pathlib.Path(state_value)
        if not state_root.is_absolute():
            state_root = (worker / state_root).resolve()
        data_value = args.data_dir or os.environ.get("AO_DATA_DIR") or "~/.ao/data"
        data_root = pathlib.Path(data_value).expanduser().resolve()
        if state_root == worker or main_common not in state_root.parents:
            raise SidecarError(
                "UNSAFE_STATE_PATH", "Sidecar state must be inside Git metadata"
            )
        return cls(repo=repo, worker=worker, state_root=state_root, data_root=data_root)

    def state(self) -> pathlib.Path:
        return self.state_root / "state.json"

    def round_dir(self, candidate_sha: str, round_number: int) -> pathlib.Path:
        return (
            self.state_root / "candidates" / candidate_sha / f"round-{round_number:02d}"
        )


def candidate_identity(
    paths: Paths, candidate_ref: str, base_ref: str
) -> dict[str, str]:
    if git(paths.worker, "status", "--porcelain=v1", "-z"):
        raise SidecarError(
            "WORKER_DIRTY", "worker worktree must be clean before review"
        )
    candidate_sha = git(paths.worker, "rev-parse", f"{candidate_ref}^{{commit}}")
    head_sha = git(paths.worker, "rev-parse", "HEAD")
    if candidate_sha != head_sha:
        raise SidecarError(
            "CANDIDATE_NOT_HEAD",
            "candidate must equal worker HEAD",
            candidate_sha=candidate_sha,
            head_sha=head_sha,
        )
    base_sha = git(paths.worker, "rev-parse", f"{base_ref}^{{commit}}")
    merge_base_sha = git(paths.worker, "merge-base", base_sha, candidate_sha)
    tree_sha = git(paths.worker, "rev-parse", f"{candidate_sha}^{{tree}}")
    remote = git(paths.worker, "remote", "get-url", "origin", check=False)
    repository = normalize_repository(remote, paths.repo.name)
    return {
        "repository": repository,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "candidate_sha": candidate_sha,
        "tree_sha": tree_sha,
        "head_sha": head_sha,
    }


def create_bundle(
    paths: Paths,
    identity: dict[str, str],
    validations: list[str],
    browser_evidence: list[str],
) -> dict[str, Any]:
    range_ref = f"{identity['merge_base_sha']}..{identity['candidate_sha']}"
    diff = git(
        paths.worker, "diff", "--no-ext-diff", "--find-renames", "--binary", range_ref
    )
    log = git(paths.worker, "log", "--format=%H%x09%aI%x09%s", range_ref)
    validation_records = [
        evidence_record(value, identity["candidate_sha"], "validation")
        for value in validations
    ]
    browser_records = [
        evidence_record(value, identity["candidate_sha"], "browser")
        for value in browser_evidence
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": identity["candidate_sha"],
        "tree_sha": identity["tree_sha"],
        "range": range_ref,
        "changed_files": changed_files(paths.worker, range_ref),
        "diff": diff,
        "diff_sha256": sha256_bytes(diff.encode()),
        "log": log,
        "worker_status": "clean",
        "validation_receipts": validation_records,
        "browser_evidence": browser_records,
        "created_at": utc_now(),
    }


def current_round(paths: Paths) -> tuple[dict[str, Any], pathlib.Path]:
    state = load_json(paths.state())
    candidate = state.get("current_candidate_sha")
    round_number = state.get("current_round")
    if not isinstance(candidate, str) or not isinstance(round_number, int):
        raise SidecarError("INVALID_STATE", "state.json has no current candidate/round")
    return state, paths.round_dir(candidate, round_number)


def command_start(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    dispatch_id = safe_segment(args.dispatch_id, "dispatch_id")
    worker_session = safe_segment(args.worker_session_id, "worker_session_id")
    if args.model != DEFAULT_MODEL:
        raise SidecarError(
            "REVIEWER_MODEL_MISMATCH",
            "the only supported external reviewer model is k3",
            expected=DEFAULT_MODEL,
            actual=args.model,
        )
    identity = candidate_identity(paths, args.candidate, args.base)
    with directory_lock(paths.state_root):
        previous: dict[str, Any] = {}
        if paths.state().exists():
            previous = load_json(paths.state())
        if previous.get("dispatch_id") not in (None, dispatch_id):
            raise SidecarError("DISPATCH_CONFLICT", "state belongs to another dispatch")
        if previous.get("current_candidate_sha") == identity[
            "candidate_sha"
        ] and isinstance(previous.get("current_round"), int):
            round_number = int(previous["current_round"])
            round_dir = paths.round_dir(identity["candidate_sha"], round_number)
            request = load_json(round_dir / "request.json")
            return {
                "status": "existing",
                "request": request,
                "round_dir": str(round_dir),
            }

        history = (
            list(previous.get("history", []))
            if isinstance(previous.get("history", []), list)
            else []
        )
        if previous.get("current_candidate_sha"):
            cleanup_previous_reviewer(paths, previous)
            history.append(
                {
                    "candidate_sha": previous["current_candidate_sha"],
                    "round": previous.get("current_round"),
                    "status": "superseded",
                    "superseded_at": utc_now(),
                }
            )
        round_number = 1
        existing_candidate = paths.state_root / "candidates" / identity["candidate_sha"]
        if existing_candidate.exists():
            numbers = [
                int(match.group(1))
                for item in existing_candidate.iterdir()
                if (match := re.fullmatch(r"round-(\d+)", item.name))
            ]
            round_number = (max(numbers) + 1) if numbers else 1

        round_dir = paths.round_dir(identity["candidate_sha"], round_number)
        if round_dir.exists():
            raise SidecarError("ROUND_EXISTS", f"round already exists: {round_dir}")
        ensure_directory(round_dir)
        request = {
            "schema_version": SCHEMA_VERSION,
            "review_profile_version": REVIEW_PROFILE_VERSION,
            **identity,
            "dispatch_id": dispatch_id,
            "worker_session_id": worker_session,
            "worker_worktree": str(paths.worker),
            "base_ref": args.base,
            "round": round_number,
            "reviewer": REVIEWER,
            "reviewer_model": args.model,
            "roborev_version": ROBOREV_VERSION,
            "created_at": utc_now(),
        }
        bundle = create_bundle(
            paths, identity, args.validation_file, args.browser_evidence_file
        )
        state = {
            "schema_version": SCHEMA_VERSION,
            "dispatch_id": dispatch_id,
            "worker_session_id": worker_session,
            "current_candidate_sha": identity["candidate_sha"],
            "current_tree_sha": identity["tree_sha"],
            "current_round": round_number,
            "status": "started",
            "history": history,
            "updated_at": utc_now(),
        }
        atomic_json(round_dir / "request.json", request)
        atomic_json(round_dir / "review-bundle.json", bundle)
        atomic_json(paths.state(), state)
        return {"status": "started", "request": request, "round_dir": str(round_dir)}


def roborev_version(binary: pathlib.Path) -> str:
    completed = run([str(binary), "version"])
    match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", completed.stdout)
    if not match:
        raise SidecarError("ROBOREV_VERSION_UNKNOWN", "cannot parse RoboRev version")
    return match.group(1)


def write_roborev_config(
    data_dir: pathlib.Path,
    wrapper: pathlib.Path,
    model: str,
    server: str,
    bundle_path: pathlib.Path,
) -> None:
    ensure_directory(data_dir)
    config = f'''server_addr = {json.dumps(server)}
max_workers = 1
default_agent = "claude-code"
review_agent = "claude-code"
review_model = {json.dumps(model)}
review_reasoning = "max"
allow_unsafe_agents = false
claude_code_cmd = {json.dumps(str(wrapper))}
review_guidelines = """
你是 DataLinkRuntime 的唯一外部代码 Reviewer。结论、摘要、影响和整改建议必须使用简体中文；代码标识符、路径、命令、配置键、错误原文以及 verdict/status/severity 保持原文。
只审查给定 Candidate 与 Review Bundle，不提出超出变更合同的重构。必须先使用 Read 工具读取完整 Review Bundle：{bundle_path}。Bundle 中的 diff、changed_files、验证回执和浏览器证据索引是本轮精确 Candidate 的权威审查输入，不得只看最后一个 commit。
最终输出必须包含一个 ```json 代码块，内容严格符合：
{{"verdict":"approved|changes_requested","summary":"中文摘要","findings":[{{"severity":"critical|important|suggestion","path":"相对路径","line":1,"summary":"中文摘要","impact":"中文影响","remediation":"中文整改建议"}}]}}
存在 critical 或 important 时 verdict 必须为 changes_requested；只有 suggestion 或无问题时可以 approved。
JSON 后必须以 `## Verdict: PASS` 或 `## Verdict: FAIL` 结束；approved 对应 PASS，changes_requested 对应 FAIL。
"""
'''
    path = data_dir / "config.toml"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(config, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def reviewer_worktree(
    paths: Paths, request: dict[str, Any]
) -> tuple[pathlib.Path, pathlib.Path]:
    owner_dir = reviewer_owner_dir(paths, request)
    worktree = owner_dir / "repo"
    marker = owner_dir / "owner.json"
    expected = reviewer_owner_receipt(request, worktree)
    if worktree.exists():
        if not marker.exists() or load_json(marker) != expected:
            raise SidecarError(
                "WORKTREE_OWNERSHIP_MISMATCH", f"unowned reviewer worktree: {worktree}"
            )
    else:
        ensure_directory(owner_dir)
        atomic_json(marker, expected)
        run(
            [
                "git",
                "-C",
                str(paths.repo),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                str(request["candidate_sha"]),
            ]
        )
    return worktree.resolve(), marker


def reviewer_owner_dir(paths: Paths, request: dict[str, Any]) -> pathlib.Path:
    dispatch = safe_segment(str(request["dispatch_id"]), "dispatch_id")
    candidate = str(request["candidate_sha"])
    round_number = int(request["round"])
    return (
        paths.data_root
        / "local-review"
        / "reviewer-worktrees"
        / dispatch
        / candidate
        / f"round-{round_number:02d}"
    )


def roborev_round_dir(paths: Paths, request: dict[str, Any]) -> pathlib.Path:
    dispatch = safe_segment(str(request["dispatch_id"]), "dispatch_id")
    candidate = safe_segment(str(request["candidate_sha"]), "candidate_sha")
    round_number = int(request["round"])
    return (
        paths.data_root
        / "local-review"
        / "roborev"
        / dispatch
        / candidate
        / f"round-{round_number:02d}"
    )


def reviewer_owner_receipt(
    request: dict[str, Any], worktree: pathlib.Path
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dispatch_id": safe_segment(str(request["dispatch_id"]), "dispatch_id"),
        "candidate_sha": str(request["candidate_sha"]),
        "round": int(request["round"]),
        "repository": request["repository"],
        "worktree": str(worktree),
    }


def verify_reviewer_worktree(
    worktree: pathlib.Path, request: dict[str, Any]
) -> dict[str, Any]:
    head = git(worktree, "rev-parse", "HEAD")
    tree = git(worktree, "rev-parse", "HEAD^{tree}")
    status = git(worktree, "status", "--porcelain=v1", "-z")
    result = {
        "worktree": str(worktree),
        "head_sha": head,
        "tree_sha": tree,
        "clean": status == "",
    }
    if head != request["candidate_sha"] or tree != request["tree_sha"] or status:
        raise SidecarError(
            "REVIEWER_WORKTREE_MISMATCH", "reviewer worktree identity changed", **result
        )
    return result


def wait_for_daemon(
    binary: pathlib.Path, server: str, env: dict[str, str], timeout: int = 20
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = run(
            [str(binary), "--server", server, "daemon", "status"],
            env=env,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            return
        time.sleep(0.25)
    raise SidecarError("ROBOREV_DAEMON_TIMEOUT", "RoboRev daemon did not become ready")


def enqueue_review(
    binary: pathlib.Path,
    server: str,
    env: dict[str, str],
    worktree: pathlib.Path,
    request: dict[str, Any],
    database: pathlib.Path,
) -> int:
    previous_id = latest_job_id(database)
    completed = run(
        [
            str(binary),
            "--server",
            server,
            "review",
            "--repo",
            str(worktree),
            "--agent",
            REVIEWER,
            "--model",
            str(request["reviewer_model"]),
            "--reasoning",
            "max",
            str(request["candidate_sha"]),
        ],
        env=env,
        timeout=30,
    )
    match = re.search(r"Enqueued job (\d+)", completed.stdout)
    if match:
        return int(match.group(1))
    persisted = find_roborev_job(
        database, str(request["candidate_sha"]), worktree, minimum_id=previous_id + 1
    )
    if persisted is None:
        raise SidecarError(
            "ROBOREV_ENQUEUE_NOT_PERSISTED",
            "RoboRev enqueue returned without a unique persisted job",
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    return int(persisted["id"])


def wait_for_event(
    stream: subprocess.Popen[str],
    job_id: int,
    event_log: pathlib.Path,
    timeout: int,
    database: pathlib.Path,
    candidate_sha: str,
    worktree: pathlib.Path,
) -> dict[str, Any]:
    selector = selectors.DefaultSelector()
    if stream.stdout is None:
        raise SidecarError("ROBOREV_STREAM_FAILED", "RoboRev stream has no stdout")
    selector.register(stream.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    with event_log.open("a", encoding="utf-8") as output:
        while time.monotonic() < deadline:
            events = selector.select(
                timeout=min(1.0, max(0.0, deadline - time.monotonic()))
            )
            if not events:
                persisted = find_roborev_job(database, candidate_sha, worktree)
                if persisted is not None and int(persisted["id"]) == job_id:
                    if persisted["status"] == "done":
                        record = query_roborev_database(database, job_id)
                        return {
                            "type": "review.completed",
                            "job_id": job_id,
                            "job_uuid": record["job_uuid"],
                            "sha": record["git_ref"],
                            "agent": record["agent"],
                            "verdict": "P" if record["verdict_bool"] == 1 else "F",
                            "worktree_path": record["worktree_path"],
                            "recovered_from_database": True,
                        }
                    if persisted["status"] in {"failed", "canceled"}:
                        raise SidecarError(
                            "ROBOREV_REVIEW_FAILED",
                            "RoboRev review did not complete",
                            job_id=job_id,
                            status=persisted["status"],
                        )
                if stream.poll() is not None:
                    break
                continue
            line = stream.stdout.readline()
            if not line:
                break
            output.write(line)
            output.flush()
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("job_id") != job_id:
                continue
            if event.get("type") == "review.completed":
                return event
            if event.get("type") in {"review.failed", "review.canceled"}:
                raise SidecarError(
                    "ROBOREV_REVIEW_FAILED",
                    "RoboRev review did not complete",
                    event=event,
                )
    raise SidecarError(
        "ROBOREV_EVENT_TIMEOUT", "timed out waiting for review.completed", job_id=job_id
    )


def sqlite_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def latest_job_id(database: pathlib.Path) -> int:
    if not database.exists():
        return 0
    connection = sqlite3.connect(database, timeout=10)
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM review_jobs"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def find_roborev_job(
    database: pathlib.Path,
    candidate_sha: str,
    worktree: pathlib.Path,
    *,
    minimum_id: int = 0,
) -> dict[str, Any] | None:
    if not database.exists():
        return None
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        job_columns = sqlite_columns(connection, "review_jobs")
        review_columns = sqlite_columns(connection, "reviews")
        if "worktree_path" not in job_columns:
            raise SidecarError(
                "ROBOREV_SCHEMA_MISMATCH", "review_jobs.worktree_path is required"
            )
        verdict_selection = (
            "r.verdict_bool AS verdict_bool"
            if "verdict_bool" in review_columns
            else "NULL AS verdict_bool"
        )
        rows = connection.execute(
            f"""
            SELECT j.id, j.uuid, j.git_ref, j.status, j.agent, j.worktree_path,
                   {verdict_selection}
            FROM review_jobs j
            LEFT JOIN reviews r ON r.job_id = j.id
            WHERE j.id >= ? AND j.git_ref = ? AND j.agent = ? AND j.worktree_path = ?
            ORDER BY j.id DESC, r.id DESC
            """,
            (minimum_id, candidate_sha, REVIEWER, str(worktree)),
        ).fetchall()
        if not rows:
            return None
        return dict(rows[0])
    finally:
        connection.close()


def review_job_needs_enqueue(job: dict[str, Any] | None) -> bool:
    return job is None or job.get("status") in {"failed", "canceled"}


def query_roborev_database(database: pathlib.Path, job_id: int) -> dict[str, Any]:
    if not database.is_file():
        raise SidecarError(
            "ROBOREV_DB_NOT_FOUND", f"RoboRev database not found: {database}"
        )
    uri = f"file:{urllib.parse.quote(str(database))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        job_columns = sqlite_columns(connection, "review_jobs")
        review_columns = sqlite_columns(connection, "reviews")
        selections = [
            "j.id AS job_id",
            "j.git_ref",
            "j.agent",
            "j.model",
            "j.status",
            "j.session_id",
            "j.finished_at",
            "j.worktree_path",
            "p.root_path AS repo_path",
            "r.output",
            "r.created_at AS review_created_at",
        ]
        selections.append(
            "j.uuid AS job_uuid"
            if "uuid" in job_columns
            else "CAST(j.id AS TEXT) AS job_uuid"
        )
        selections.append(
            "r.uuid AS review_uuid"
            if "uuid" in review_columns
            else "CAST(r.id AS TEXT) AS review_uuid"
        )
        selections.append(
            "r.verdict_bool"
            if "verdict_bool" in review_columns
            else "NULL AS verdict_bool"
        )
        sql = f"""
            SELECT {", ".join(selections)}
            FROM review_jobs j
            JOIN repos p ON p.id = j.repo_id
            JOIN reviews r ON r.job_id = j.id
            WHERE j.id = ?
        """
        row = connection.execute(sql, (job_id,)).fetchone()
        if row is None:
            raise SidecarError(
                "ROBOREV_RECORD_NOT_FOUND",
                "completed RoboRev record not found",
                job_id=job_id,
            )
        result = dict(row)
        result["output_sha256"] = sha256_bytes(str(result["output"]).encode())
        return result
    finally:
        connection.close()


def validate_audit(
    audit: dict[str, Any], expected_bundle_dir: pathlib.Path | None = None
) -> dict[str, Any]:
    effective = audit.get("effective_argv")
    if not isinstance(effective, list):
        raise SidecarError(
            "REVIEW_PROFILE_MISMATCH", "Claude audit has no effective argv"
        )
    joined = "\0".join(str(item) for item in effective)
    required = [
        "--tools",
        "Read,Glob,Grep",
        "--permission-mode",
        "dontAsk",
        "--safe-mode",
        "--strict-mcp-config",
    ]
    missing = [value for value in required if value not in effective]
    forbidden = [
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "bypassPermissions",
        "Edit,MultiEdit,Write",
    ]
    present_forbidden = [value for value in forbidden if value in joined]
    if missing or present_forbidden:
        raise SidecarError(
            "REVIEW_PROFILE_MISMATCH",
            "Claude effective argv violates the review profile",
            missing=missing,
            forbidden=present_forbidden,
        )
    if expected_bundle_dir is not None:
        add_dir_indexes = [
            index for index, value in enumerate(effective) if value == "--add-dir"
        ]
        if len(add_dir_indexes) != 1 or add_dir_indexes[0] + 1 >= len(effective):
            raise SidecarError(
                "REVIEW_PROFILE_MISMATCH",
                "Claude must receive exactly one bundle --add-dir",
            )
        actual_bundle_dir = pathlib.Path(
            str(effective[add_dir_indexes[0] + 1])
        ).resolve()
        if actual_bundle_dir != expected_bundle_dir.resolve():
            raise SidecarError(
                "REVIEW_PROFILE_MISMATCH",
                "Claude bundle directory does not match the current round",
                expected=str(expected_bundle_dir.resolve()),
                actual=str(actual_bundle_dir),
            )
    init_event = audit.get("init_event")
    if not isinstance(init_event, dict):
        raise SidecarError("CLAUDE_INIT_MISSING", "Claude init event was not captured")
    tools = init_event.get("tools")
    permission = init_event.get("permissionMode", init_event.get("permission_mode"))
    if set(tools or []) != {"Read", "Glob", "Grep"} or permission != "dontAsk":
        raise SidecarError(
            "CLAUDE_INIT_MISMATCH",
            "Claude init tools or permission mode do not match",
            tools=tools,
            permission_mode=permission,
        )
    for key in ("mcp_servers", "plugins", "skills", "slash_commands"):
        value = init_event.get(key, [])
        if value not in (None, [], {}):
            raise SidecarError(
                "CLAUDE_INIT_MISMATCH", f"Claude init {key} must be empty", value=value
            )
    if audit.get("exit_code") != 0:
        raise SidecarError(
            "CLAUDE_FAILED",
            "Claude wrapper reported a nonzero exit",
            exit_code=audit.get("exit_code"),
        )
    return {
        "profile_version": REVIEW_PROFILE_VERSION,
        "effective_argv_sha256": sha256_bytes(
            json.dumps(effective, sort_keys=True).encode()
        ),
        "tools": sorted(tools),
        "permission_mode": permission,
        "safe_mode": True,
    }


def extract_structured_review(output: str) -> dict[str, Any]:
    candidates = re.findall(
        r"```(?:json)?\s*(\{.*?\})\s*```", output, flags=re.DOTALL | re.IGNORECASE
    )
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("verdict") in {
            "approved",
            "changes_requested",
        }:
            findings = value.get("findings")
            if not isinstance(findings, list):
                raise SidecarError(
                    "INVALID_REVIEW_OUTPUT", "structured findings must be an array"
                )
            normalized: list[dict[str, Any]] = []
            for index, finding in enumerate(findings, 1):
                if not isinstance(finding, dict):
                    raise SidecarError(
                        "INVALID_REVIEW_OUTPUT", "each finding must be an object"
                    )
                severity = finding.get("severity")
                if severity not in {"critical", "important", "suggestion"}:
                    raise SidecarError(
                        "INVALID_REVIEW_OUTPUT",
                        f"invalid finding severity: {severity!r}",
                    )
                path = str(finding.get("path", ""))
                line = finding.get("line")
                if path.startswith("/") or ".." in pathlib.PurePosixPath(path).parts:
                    raise SidecarError(
                        "INVALID_REVIEW_OUTPUT",
                        f"finding path must be repository-relative: {path}",
                    )
                fingerprint_source = json.dumps(
                    [severity, path, line, finding.get("summary", "")],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                normalized.append(
                    {
                        "id": sha256_bytes(fingerprint_source.encode())[:20],
                        "severity": severity,
                        "path": path,
                        "line": line if isinstance(line, int) and line > 0 else None,
                        "summary": str(finding.get("summary", "")),
                        "impact": str(finding.get("impact", "")),
                        "remediation": str(finding.get("remediation", "")),
                    }
                )
            return {
                "verdict": value["verdict"],
                "summary": str(value.get("summary", "")),
                "findings": normalized,
            }
    raise SidecarError(
        "INVALID_REVIEW_OUTPUT", "RoboRev output has no valid structured review JSON"
    )


def build_gate(
    request: dict[str, Any],
    roborev: dict[str, Any],
    audit_summary: dict[str, Any],
    structured: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    event = roborev["event"]
    database = roborev["database"]
    identity_errors: list[str] = []
    expected = {
        "git_ref": request["candidate_sha"],
        "agent": REVIEWER,
        "status": "done",
    }
    for key, value in expected.items():
        if database.get(key) != value:
            identity_errors.append(f"database.{key}")
    if event.get("job_uuid") != database.get("job_uuid"):
        identity_errors.append("event.job_uuid")
    if event.get("sha") != request["candidate_sha"]:
        identity_errors.append("event.sha")
    if event.get("agent") != REVIEWER:
        identity_errors.append("event.agent")

    verdict = structured["verdict"]
    if event.get("verdict") == "F" or database.get("verdict_bool") == 0:
        verdict = "changes_requested"
    if any(item["severity"] in BLOCKING_SEVERITIES for item in structured["findings"]):
        verdict = "changes_requested"
    if identity_errors:
        verdict = "unknown"
    claude = {
        "schema_version": SCHEMA_VERSION,
        "review_profile_version": REVIEW_PROFILE_VERSION,
        "reviewer": REVIEWER,
        "reviewer_model": request["reviewer_model"],
        "summary": structured["summary"],
        "verdict": verdict,
        "findings": structured["findings"],
        "profile": audit_summary,
        "identity_errors": identity_errors,
        "completed_at": utc_now(),
    }
    gate = {
        "schema_version": SCHEMA_VERSION,
        "review_profile_version": REVIEW_PROFILE_VERSION,
        "repository": request["repository"],
        "dispatch_id": request["dispatch_id"],
        "worker_session_id": request["worker_session_id"],
        "reviewer": REVIEWER,
        "reviewer_model": request["reviewer_model"],
        "reviewer_session_id": database.get("session_id") or "",
        "roborev_job_uuid": database["job_uuid"],
        "roborev_review_uuid": database["review_uuid"],
        "base_sha": request["base_sha"],
        "merge_base_sha": request["merge_base_sha"],
        "candidate_sha": request["candidate_sha"],
        "tree_sha": request["tree_sha"],
        "round": request["round"],
        "verdict": verdict,
        "findings": structured["findings"],
        "completed_at": utc_now(),
    }
    return claude, gate


def delivery_message(gate: dict[str, Any]) -> str:
    return (
        "LOCAL_REVIEW_RESULT "
        f"DISPATCH_ID={gate['dispatch_id']} "
        f"CANDIDATE_SHA={gate['candidate_sha']} "
        f"ROUND={gate['round']} VERDICT={gate['verdict']} "
        f"FINDINGS={len(gate['findings'])}"
    )


def deliver(
    round_dir: pathlib.Path, gate: dict[str, Any], ao_session: str | None, ao_bin: str
) -> dict[str, Any]:
    delivery_path = round_dir / "delivery.json"
    previous = load_json(delivery_path) if delivery_path.exists() else {}
    attempts = int(previous.get("attempts", 0))
    payload_sha256 = sha256_bytes(delivery_message(gate).encode())
    if previous.get("payload_sha256") not in (None, payload_sha256):
        raise SidecarError(
            "DELIVERY_CONFLICT", "delivery payload conflicts with the persisted receipt"
        )
    if previous.get("status") == "delivered":
        return previous
    if not ao_session:
        result = {
            "schema_version": 1,
            "status": "not_requested",
            "target_session": None,
            "attempts": attempts,
            "payload_sha256": payload_sha256,
            "updated_at": utc_now(),
        }
        atomic_json(delivery_path, result)
        return result
    pending = {
        "schema_version": 1,
        "status": "pending",
        "target_session": ao_session,
        "attempts": attempts,
        "payload_sha256": payload_sha256,
        "last_error": previous.get("last_error"),
        "updated_at": utc_now(),
    }
    atomic_json(delivery_path, pending)
    completed = run(
        [ao_bin, "send", "--session", ao_session, "--message", delivery_message(gate)],
        check=False,
    )
    attempts += 1
    if completed.returncode == 0:
        result = {
            **pending,
            "status": "delivered",
            "attempts": attempts,
            "last_error": None,
            "delivered_at": utc_now(),
            "updated_at": utc_now(),
        }
    else:
        result = {
            **pending,
            "attempts": attempts,
            "last_error": (completed.stderr or completed.stdout)[-2000:],
            "updated_at": utc_now(),
        }
    atomic_json(delivery_path, result)
    return result


def clean_reviewer_worktree(
    paths: Paths, worktree: pathlib.Path, marker: pathlib.Path
) -> None:
    owner = load_json(marker)
    if pathlib.Path(str(owner.get("worktree", ""))).resolve() != worktree.resolve():
        raise SidecarError(
            "WORKTREE_OWNERSHIP_MISMATCH",
            "refusing to remove unowned reviewer worktree",
        )
    expected_root = (paths.data_root / "local-review" / "reviewer-worktrees").resolve()
    if expected_root not in worktree.resolve().parents:
        raise SidecarError(
            "UNSAFE_WORKTREE_PATH",
            f"reviewer worktree is outside managed root: {worktree}",
        )
    run(["git", "-C", str(paths.repo), "worktree", "remove", str(worktree)])
    shutil.rmtree(marker.parent)


def cleanup_previous_reviewer(paths: Paths, state: dict[str, Any]) -> None:
    candidate = state.get("current_candidate_sha")
    round_number = state.get("current_round")
    if not isinstance(candidate, str) or not isinstance(round_number, int):
        return
    request_path = paths.round_dir(candidate, round_number) / "request.json"
    if not request_path.exists():
        return
    request = load_json(request_path)
    owner_dir = reviewer_owner_dir(paths, request)
    worktree = owner_dir / "repo"
    marker = owner_dir / "owner.json"
    if worktree.exists():
        if not marker.exists():
            raise SidecarError(
                "WORKTREE_OWNERSHIP_MISMATCH",
                f"refusing to clean unowned reviewer worktree: {worktree}",
            )
        clean_reviewer_worktree(paths, worktree.resolve(), marker)
    elif marker.exists():
        expected = reviewer_owner_receipt(request, worktree)
        if load_json(marker) != expected:
            raise SidecarError(
                "WORKTREE_OWNERSHIP_MISMATCH",
                f"refusing to clean invalid reviewer owner directory: {owner_dir}",
            )
        shutil.rmtree(owner_dir)
    roborev_dir = roborev_round_dir(paths, request)
    if roborev_dir.exists():
        shutil.rmtree(roborev_dir)


def command_run(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    with directory_lock(paths.state_root):
        state, round_dir = current_round(paths)
        request = load_json(round_dir / "request.json")
        identity = candidate_identity(
            paths, request["candidate_sha"], request["base_sha"]
        )
        if identity["tree_sha"] != request["tree_sha"]:
            raise SidecarError(
                "CANDIDATE_MISMATCH", "candidate tree changed since start"
            )
        if (round_dir / "gate.json").exists():
            gate = load_json(round_dir / "gate.json")
            delivery_path = round_dir / "delivery.json"
            delivery = (
                load_json(delivery_path)
                if delivery_path.exists()
                else deliver(round_dir, gate, args.ao_session, args.ao_bin)
            )
            return {
                "status": "existing",
                "gate": gate,
                "delivery": delivery,
            }
        binary = pathlib.Path(args.roborev_bin).expanduser().resolve()
        wrapper = pathlib.Path(args.claude_wrapper).expanduser().resolve()
        real_claude = pathlib.Path(args.real_claude).expanduser().resolve()
        for path, code in (
            (binary, "ROBOREV_NOT_FOUND"),
            (wrapper, "WRAPPER_NOT_FOUND"),
            (real_claude, "CLAUDE_NOT_FOUND"),
        ):
            if not path.is_file():
                raise SidecarError(code, f"required executable not found: {path}")
        actual_version = roborev_version(binary)
        if actual_version != ROBOREV_VERSION:
            raise SidecarError(
                "ROBOREV_VERSION_MISMATCH",
                "RoboRev version is not pinned",
                expected=ROBOREV_VERSION,
                actual=actual_version,
            )

        worktree, marker = reviewer_worktree(paths, request)
        before = verify_reviewer_worktree(worktree, request)
        roborev_dir = ensure_directory(roborev_round_dir(paths, request))
        ensure_directory(roborev_dir / "runtime")
        socket = (
            pathlib.Path(tempfile.gettempdir())
            / f"ao-lr-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        )
        server = f"unix://{socket}"
        audit_file = round_dir / "claude-audit.json"
        empty_mcp = round_dir / "empty-mcp.json"
        empty_settings = round_dir / "empty-settings.json"
        atomic_json(empty_mcp, {"mcpServers": {}})
        atomic_json(empty_settings, {"hooks": {}, "enabledPlugins": {}})
        env = os.environ.copy()
        env.update(
            {
                "ROBOREV_DATA_DIR": str(roborev_dir),
                "ROBOREV_TELEMETRY_ENABLED": "0",
                "AO_LOCAL_REVIEW_REAL_CLAUDE": str(real_claude),
                "AO_LOCAL_REVIEW_AUDIT_FILE": str(audit_file),
                "AO_LOCAL_REVIEW_EMPTY_MCP": str(empty_mcp),
                "AO_LOCAL_REVIEW_EMPTY_SETTINGS": str(empty_settings),
                "AO_LOCAL_REVIEW_BUNDLE_DIR": str(round_dir.resolve()),
                "AO_LOCAL_REVIEW_MODEL": str(request["reviewer_model"]),
                "AO_LOCAL_REVIEW_PROFILE_VERSION": str(REVIEW_PROFILE_VERSION),
            }
        )
        daemon_log = roborev_dir / "logs" / f"sidecar-{request['dispatch_id']}.log"
        ensure_directory(daemon_log.parent)
        with directory_lock(roborev_dir), daemon_log.open("ab") as log_handle:
            write_roborev_config(
                roborev_dir,
                wrapper,
                request["reviewer_model"],
                server,
                (round_dir / "review-bundle.json").resolve(),
            )
            daemon = subprocess.Popen(
                [str(binary), "--server", server, "daemon", "run"],
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            stream: subprocess.Popen[str] | None = None
            try:
                wait_for_daemon(binary, server, env)
                stream = subprocess.Popen(
                    [
                        str(binary),
                        "--server",
                        server,
                        "stream",
                        "--repo",
                        str(worktree),
                    ],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                database_path = roborev_dir / "reviews.db"
                existing_job = find_roborev_job(
                    database_path, str(request["candidate_sha"]), worktree
                )
                if review_job_needs_enqueue(existing_job):
                    job_id = enqueue_review(
                        binary, server, env, worktree, request, database_path
                    )
                    event = wait_for_event(
                        stream,
                        job_id,
                        round_dir / "roborev-events.jsonl",
                        args.timeout,
                        database_path,
                        str(request["candidate_sha"]),
                        worktree,
                    )
                else:
                    job_id = int(existing_job["id"])
                    if existing_job["status"] == "done":
                        event = {
                            "type": "review.completed",
                            "job_id": job_id,
                            "job_uuid": existing_job["uuid"],
                            "sha": existing_job["git_ref"],
                            "agent": existing_job["agent"],
                            "verdict": "P"
                            if existing_job["verdict_bool"] == 1
                            else "F",
                            "worktree_path": existing_job["worktree_path"],
                            "recovered_from_database": True,
                        }
                    else:
                        event = wait_for_event(
                            stream,
                            job_id,
                            round_dir / "roborev-events.jsonl",
                            args.timeout,
                            database_path,
                            str(request["candidate_sha"]),
                            worktree,
                        )
            finally:
                if stream is not None and stream.poll() is None:
                    stream.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        stream.wait(timeout=5)
                with contextlib.suppress(subprocess.TimeoutExpired, SidecarError):
                    run(
                        [str(binary), "--server", server, "daemon", "stop"],
                        env=env,
                        check=False,
                        timeout=3,
                    )
                if daemon.poll() is None:
                    daemon.terminate()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        daemon.wait(timeout=5)
                    if daemon.poll() is None:
                        daemon.kill()
                with contextlib.suppress(FileNotFoundError):
                    socket.unlink()

        database_record = query_roborev_database(roborev_dir / "reviews.db", job_id)
        if pathlib.Path(str(database_record["repo_path"])).resolve() != paths.repo:
            raise SidecarError(
                "ROBOREV_REPOSITORY_MISMATCH",
                "RoboRev persisted a different repository root",
            )
        if pathlib.Path(str(database_record["worktree_path"])).resolve() != worktree:
            raise SidecarError(
                "ROBOREV_WORKTREE_MISMATCH", "RoboRev persisted a different worktree"
            )
        audit = load_json(audit_file)
        audit_summary = validate_audit(audit, round_dir)
        after = verify_reviewer_worktree(worktree, request)
        if (
            event.get("worktree_path")
            and pathlib.Path(str(event["worktree_path"])).resolve() != worktree
        ):
            raise SidecarError(
                "ROBOREV_WORKTREE_MISMATCH", "RoboRev event names a different worktree"
            )
        structured = extract_structured_review(str(database_record["output"]))
        roborev = {
            "schema_version": 1,
            "version": actual_version,
            "job_id": job_id,
            "event": event,
            "database": database_record,
            "worktree_before": before,
            "worktree_after": after,
            "captured_at": utc_now(),
        }
        claude, gate = build_gate(request, roborev, audit_summary, structured)
        atomic_json(round_dir / "roborev.json", roborev)
        atomic_json(round_dir / "claude.json", claude)
        atomic_json(round_dir / "gate.json", gate)
        state["status"] = "reviewed"
        state["verdict"] = gate["verdict"]
        state["updated_at"] = utc_now()
        atomic_json(paths.state(), state)
        delivery = deliver(round_dir, gate, args.ao_session, args.ao_bin)
        clean_reviewer_worktree(paths, worktree, marker)
        shutil.rmtree(roborev_dir)
        return {"status": "completed", "gate": gate, "delivery": delivery}


def command_status(_args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    state, round_dir = current_round(paths)
    result: dict[str, Any] = {"state": state, "round_dir": str(round_dir)}
    for name in ("request", "roborev", "claude", "gate", "delivery"):
        path = round_dir / f"{name}.json"
        if path.exists():
            result[name] = load_json(path)
    return result


def validate_current_gate(paths: Paths) -> tuple[dict[str, Any], pathlib.Path]:
    state, round_dir = current_round(paths)
    gate = load_json(round_dir / "gate.json")
    if state.get("current_candidate_sha") != gate.get("candidate_sha"):
        raise SidecarError("GATE_STALE", "gate is not for the current candidate")
    if gate.get("verdict") != "approved":
        raise SidecarError(
            "GATE_NOT_APPROVED",
            "current local external review is not approved",
            verdict=gate.get("verdict"),
        )
    identity = candidate_identity(
        paths, str(gate["candidate_sha"]), str(gate["base_sha"])
    )
    if identity["tree_sha"] != gate.get("tree_sha"):
        raise SidecarError(
            "GATE_STALE", "current candidate tree does not match the approved gate"
        )
    delivery_path = round_dir / "delivery.json"
    if delivery_path.exists():
        delivery = load_json(delivery_path)
        if delivery.get("target_session") and delivery.get("status") != "delivered":
            raise SidecarError(
                "DELIVERY_PENDING", "approved gate has not been delivered"
            )
    return gate, round_dir


def command_gate(_args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    gate, round_dir = validate_current_gate(paths)
    return {"status": "approved", "gate": gate, "round_dir": str(round_dir)}


def command_supersede(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    if not paths.state().exists():
        raise SidecarError("STATE_NOT_FOUND", "cannot supersede before start")
    previous = load_json(paths.state())
    new_sha = git(paths.worker, "rev-parse", f"{args.candidate}^{{commit}}")
    if new_sha == previous.get("current_candidate_sha"):
        raise SidecarError(
            "CANDIDATE_UNCHANGED", "supersede requires a new candidate SHA"
        )
    return command_start(args, paths)


def command_reconcile(args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    with directory_lock(paths.state_root):
        _state, round_dir = current_round(paths)
        gate = load_json(round_dir / "gate.json")
        delivery_path = round_dir / "delivery.json"
        if delivery_path.exists():
            previous = load_json(delivery_path)
            if (
                previous.get("status") == "pending"
                and int(previous.get("attempts", 0)) >= MAX_DELIVERY_ATTEMPTS
            ):
                raise SidecarError(
                    "DELIVERY_RETRY_EXHAUSTED",
                    f"AO delivery failed {MAX_DELIVERY_ATTEMPTS} times",
                    delivery=previous,
                )
        delivery = deliver(round_dir, gate, args.ao_session, args.ao_bin)
        if (
            delivery.get("status") == "pending"
            and int(delivery.get("attempts", 0)) >= MAX_DELIVERY_ATTEMPTS
        ):
            raise SidecarError(
                "DELIVERY_RETRY_EXHAUSTED",
                f"AO delivery failed {MAX_DELIVERY_ATTEMPTS} times",
                delivery=delivery,
            )
        return {"status": delivery["status"], "delivery": delivery}


def command_archive(_args: argparse.Namespace, paths: Paths) -> dict[str, Any]:
    with directory_lock(paths.state_root):
        gate, round_dir = validate_current_gate(paths)
        project = safe_segment(str(gate["repository"]).replace("/", "--"), "project")
        destination = (
            paths.data_root
            / "local-review-archive"
            / project
            / str(gate["dispatch_id"])
            / str(gate["candidate_sha"])
        )
        if destination.exists():
            manifest = load_json(destination / "archive.json")
            if manifest.get("gate_sha256") != sha256_file(round_dir / "gate.json"):
                raise SidecarError(
                    "ARCHIVE_CONFLICT",
                    f"archive already exists with different content: {destination}",
                )
            return {
                "status": "existing",
                "archive": str(destination),
                "manifest": manifest,
            }
        ensure_directory(destination.parent)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}")
        shutil.copytree(round_dir, temporary)
        manifest = {
            "schema_version": 1,
            "repository": gate["repository"],
            "dispatch_id": gate["dispatch_id"],
            "candidate_sha": gate["candidate_sha"],
            "gate_sha256": sha256_file(round_dir / "gate.json"),
            "archived_at": utc_now(),
        }
        atomic_json(temporary / "archive.json", manifest)
        os.replace(temporary, destination)
        state = load_json(paths.state())
        state["status"] = "archived"
        state["archive"] = str(destination)
        state["updated_at"] = utc_now()
        atomic_json(paths.state(), state)
        return {"status": "archived", "archive": str(destination), "manifest": manifest}


def emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def fail(error: SidecarError, as_json: bool) -> NoReturn:
    payload = {
        "ok": False,
        "error": {"code": error.code, "message": str(error), "details": error.details},
    }
    emit(payload, as_json)
    raise SystemExit(2)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="ao-local-review")
    result.add_argument("--repo", default=".")
    result.add_argument("--worker-worktree")
    result.add_argument("--data-dir")
    result.add_argument("--json", action="store_true")
    subparsers = result.add_subparsers(dest="command", required=True)

    def identity(command: argparse.ArgumentParser) -> None:
        command.add_argument("--dispatch-id", required=True)
        command.add_argument("--worker-session-id", required=True)
        command.add_argument("--base", default="main")
        command.add_argument("--candidate", default="HEAD")
        command.add_argument("--model", default=DEFAULT_MODEL)

    start = subparsers.add_parser("start")
    identity(start)
    start.add_argument("--validation-file", action="append", default=[])
    start.add_argument("--browser-evidence-file", action="append", default=[])

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--roborev-bin", required=True)
    run_parser.add_argument("--claude-wrapper", required=True)
    run_parser.add_argument("--real-claude", required=True)
    run_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run_parser.add_argument("--ao-session")
    run_parser.add_argument("--ao-bin", default="ao")

    subparsers.add_parser("status")
    subparsers.add_parser("gate")

    supersede = subparsers.add_parser("supersede")
    identity(supersede)
    supersede.add_argument("--validation-file", action="append", default=[])
    supersede.add_argument("--browser-evidence-file", action="append", default=[])

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--ao-session", required=True)
    reconcile.add_argument("--ao-bin", default="ao")

    subparsers.add_parser("archive")
    return result


COMMANDS = {
    "start": command_start,
    "run": command_run,
    "status": command_status,
    "gate": command_gate,
    "supersede": command_supersede,
    "reconcile": command_reconcile,
    "archive": command_archive,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        paths = Paths.resolve(args)
        payload = COMMANDS[args.command](args, paths)
        emit({"ok": True, **payload}, args.json)
        return 0
    except SidecarError as error:
        fail(error, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
