"""Bounded, local revalidation of system-owned cache rebuild receipts.

Receipts supplement the unchanged cache manifest; an older Worker ignores
them. They never make network availability or an administrator's promise an
automatic fact.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dlr.worker.cache import CacheError, CacheScanBudget, VerifiedVersionCache, _safe_key
from dlr.worker.cache_lifecycle import CacheLifecycleStore, _read_bounded_json, _write_json

_RECEIPT_LIMIT = 256 * 1024
_SOURCE_LIMIT = 1024 * 1024
_TOOLS = {
    "python": ("uv",),
    "javascript": ("node",),
    "typescript": ("node", "tsc"),
    "java": ("java", "javac"),
    "go": ("go",),
}


def _receipt_path(cache_root: Path, key: str) -> Path:
    return cache_root / ".dlr-cache-lifecycle" / f"rebuild-{_safe_key(key)}.json"


def _tools(language: str, *, packages: bool = False) -> dict[str, Any] | None:
    commands = _TOOLS.get(language)
    if commands is None:
        return None
    if packages:
        commands += {"javascript": ("npm",), "typescript": ("npm",), "java": ("mvn",)}.get(
            language, ()
        )
    paths = {command: shutil.which(command) for command in commands}
    if language == "python":
        paths["python"] = sys.executable
    if any(path is None for path in paths.values()):
        return None
    result: dict[str, Any] = {"platform": [platform.system(), platform.machine()]}
    if language == "python":
        result["python_version"] = sys.version
    try:
        for command, executable in paths.items():
            assert executable is not None
            path = Path(executable).resolve(strict=True)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
                return None
            result[command] = [
                str(path),
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ]
        if language == "typescript":
            from dlr.runtime.typescript_runtime import DECLARATIONS

            compiler = Path(str(paths["tsc"])).resolve(strict=True)
            package = compiler.parents[1]
            support = {}
            for relative in ("package.json", "lib/tsc.js", "lib/_tsc.js"):
                path = package / relative
                if relative == "lib/_tsc.js" and not path.exists():
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    return None
                support[relative] = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
            result["typescript_support"] = support
            result["typescript_declarations"] = hashlib.sha256(DECLARATIONS.encode()).hexdigest()
        if language == "go":
            from dlr.runtime.go_runtime import SOURCE

            result["go_harness"] = hashlib.sha256(SOURCE.encode()).hexdigest()
    except OSError:
        return None
    return result


def _read_file(
    path: Path, *, budget: CacheScanBudget, limit: int, collect: bool = True
) -> tuple[bytes, int, str]:
    budget.consume_node()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > limit:
        raise CacheError("cache_rebuild_unknown")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (
            (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size > limit
        ):
            raise CacheError("cache_rebuild_unknown")
        content = bytearray()
        total = 0
        hasher = hashlib.sha256()
        while chunk := stream.read(min(64 * 1024, limit + 1 - total)):
            budget.consume_hash_bytes(len(chunk))
            total += len(chunk)
            hasher.update(chunk)
            if collect:
                content.extend(chunk)
            if total > limit:
                raise CacheError("cache_rebuild_unknown")
        return bytes(content), total, hasher.hexdigest()


def _materials_valid(receipt: Mapping[str, Any], budget: CacheScanBudget) -> bool:
    files = receipt.get("files")
    if files == []:
        return receipt.get("directory") is None
    if not isinstance(files, list) or not files or len(files) > 200:
        return False
    directory = receipt.get("directory")
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        return False
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        return False
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            return False
        relative, size, digest = item["path"], item["size"], item["sha256"]
        if (
            not isinstance(relative, str)
            or relative in seen
            or not relative
            or "\\" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            return False
        seen.add(relative)
        parent = root
        for part in relative.split("/")[:-1]:
            budget.consume_node()
            parent /= part
            if parent.is_symlink() or not parent.is_dir():
                return False
        _content, length, actual_digest = _read_file(
            root / relative, budget=budget, limit=size, collect=False
        )
        if length != size or actual_digest != digest:
            return False
    return True


def verify_receipt(
    cache_root: Path,
    key: str,
    *,
    identity: Mapping[str, Any],
    digest: str,
    budget: CacheScanBudget | None = None,
) -> bool | None:
    """None means legacy receipt absent; False is a known invalid system fact."""

    budget = budget or CacheScanBudget(1000, 16 * 1024 * 1024, time.monotonic() + 0.5)
    budget.consume_node()
    receipt = _read_bounded_json(_receipt_path(cache_root, key), max_bytes=_RECEIPT_LIMIT)
    if receipt is None:
        return None
    if (
        set(receipt) != {"schema", "identity", "digest", "tools", "directory", "files"}
        or receipt.get("schema") != 1
        or receipt.get("identity") != dict(identity)
        or receipt.get("digest") != digest
    ):
        return False
    tools = _tools(str(identity.get("language")), packages=bool(receipt["files"]))
    return tools is not None and receipt["tools"] == tools and _materials_valid(receipt, budget)


def record_rebuild_receipt(
    runtime_root: Path,
    *,
    identity: Mapping[str, Any],
    external_dependencies_present: bool,
    builtin_materials: Any = None,
) -> bool:
    """Called only after a verified prepare hit or successful promotion."""

    files: list[dict[str, Any]] = []
    directory = None
    if external_dependencies_present:
        if (
            builtin_materials is None
            or builtin_materials.lifecycle != "managed_persistent"
            or not isinstance(builtin_materials.snapshot.get("files"), list)
            or len(builtin_materials.snapshot["files"]) > 200
        ):
            return False
        for item in builtin_materials.snapshot["files"]:
            files.append(
                {
                    "path": item["repository_path"],
                    "size": item["size_bytes"],
                    "sha256": item["sha256"],
                }
            )
        if not files:
            return False
        directory = str(builtin_materials.directory.absolute())
    elif builtin_materials is not None and builtin_materials.snapshot.get("files") != []:
        # A nonempty built-in snapshot is not equivalent to no package material.
        return False
    tools = _tools(str(identity.get("language")), packages=bool(files))
    if tools is None:
        return False
    store = CacheLifecycleStore.for_runtime(runtime_root)
    key = f"{identity['adapter_id']}-{identity['version_id']}"
    with store.entry_lock(key):
        facts = store.lifecycle(key)
        if facts["identity"] != dict(identity):
            raise CacheError("cache_identity_conflict")
        manifest = VerifiedVersionCache(store.cache_root).manifest_identity(
            store.cache_root / "entries" / key
        )
        if manifest is None or manifest != (dict(identity), facts["digest"]):
            raise CacheError("cache_identity_conflict")
        if identity.get("language") == "python":
            executable = store.cache_root / "entries" / key / ".venv" / "bin" / "python"
            if executable.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
                return False
        receipt = {
            "schema": 1,
            "identity": dict(identity),
            "digest": facts["digest"],
            "tools": tools,
            "directory": directory,
            "files": files,
        }
        if len(json.dumps(receipt).encode("ascii")) > _RECEIPT_LIMIT:
            return False
        if not _materials_valid(
            receipt, CacheScanBudget(2000, 256 * 1024 * 1024, time.monotonic() + 0.5)
        ):
            return False
        _write_json(
            _receipt_path(store.cache_root, key),
            receipt,
            strict_sync=True,
        )
    return True


def revalidate_for_scan(
    store: CacheLifecycleStore,
    key: str,
    *,
    identity: Mapping[str, Any],
    digest: str,
    budget: CacheScanBudget,
    now: float,
) -> None:
    """Refresh expiring system proofs from current materials, never manual promises."""

    with store.entry_lock(key, blocking=False):
        _revalidate_for_scan_locked(
            store, key, identity=identity, digest=digest, budget=budget, now=now
        )


def _revalidate_for_scan_locked(
    store: CacheLifecycleStore,
    key: str,
    *,
    identity: Mapping[str, Any],
    digest: str,
    budget: CacheScanBudget,
    now: float,
) -> None:

    facts = store.lifecycle(key)
    if facts["identity"] != dict(identity) or facts["digest"] != digest:
        return
    proof = facts.get("rebuild_proof")
    if isinstance(proof, dict) and proof.get("automatic") is not True:
        return
    verified = verify_receipt(
        store.cache_root, key, identity=identity, digest=digest, budget=budget
    )
    if verified is None and identity.get("language") == "python":
        # Legacy Python's hash covers exactly this complete dependency declaration.
        # Built-in snapshots add identity material and deliberately do not match.
        content, _length, source_digest = _read_file(
            store.cache_root / "entries" / key / "requirements.txt",
            budget=budget,
            limit=_SOURCE_LIMIT,
        )
        requirements = content.decode("utf-8")
        if source_digest == identity.get("source_sha256") and all(
            not line.strip() or line.strip().startswith("#") for line in requirements.splitlines()
        ):
            verified = record_rebuild_receipt(
                store.cache_root.parent, identity=identity, external_dependencies_present=False
            )
    if verified is False:
        store.invalidate_automatic_rebuildability(key, identity=identity, digest=digest)
    if verified and (not isinstance(proof, dict) or float(proof["valid_until"]) <= now + 300):
        store.confirm_rebuildability(
            key,
            identity=identity,
            digest=digest,
            source_policy="verified_offline",
            evidence_note="verified local rebuild materials",
            actor="platform",
            now=now,
            valid_until=now + 86_400,
            automatic=True,
        )
