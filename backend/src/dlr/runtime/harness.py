"""Python Runtime harness (M2 Runtime Contract).

Executed by the version-scoped venv's own Python as a plain script:

    <venv>/bin/python harness.py <workspace>

Intentionally stdlib-only so it runs inside any adapter venv without extra
installs. The workspace is a per-execution directory prepared by the Worker:

    adapter.py           immutable AdapterVersion snapshot
    input.json           execution input (any JSON value, including null)
    runtime_config.json  version runtime_config (JSON object)
    output.json          written by the harness with the return value

Any exception (including a non-JSON-serializable return value) prints a
traceback to stderr and exits non-zero, which the Worker reports as failed.
"""

import importlib.util
import json
import logging
import os
import re
import stat
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

_WORKSPACE_NAME_PATTERN = re.compile(r"dlr-exec-([1-9][0-9]*)\Z")
_MOUNT_NAME_PATTERN = re.compile(r"input-([0-9]{2})\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "ordinal",
        "mount_name",
        "original_filename",
        "content_type",
        "size_bytes",
        "sha256",
    }
)


@dataclass(frozen=True, slots=True)
class InputFile:
    """Read-only metadata for one Worker-local managed input file."""

    ordinal: int
    path: Path
    original_name: str
    content_type: str
    size_bytes: int
    sha256: str


class InputManifestError(Exception):
    """Stable pre-Adapter failure while validating the Worker manifest."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Secrets:
    """Read-through access to Worker-provided ``DLR_SECRET_<KEY>`` values.

    Real secret values never pass through the Control Node or the database.
    """

    def get(self, key: str) -> str | None:
        return os.environ.get(f"DLR_SECRET_{key}")


class Context:
    """The ``context`` object passed to ``handle(context, input)``."""

    def __init__(
        self,
        config: object,
        secrets: Secrets,
        logger: logging.Logger,
        input_files: tuple[InputFile, ...] = (),
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.logger = logger
        self.input_files = tuple(input_files)


def _manifest_int(value: object, *, positive: bool = False) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if positive and value <= 0:
        return None
    return value


def _validate_input_file(path: Path) -> None:
    """Validate one Worker-verified file without reading its contents."""
    try:
        info = path.lstat()
    except OSError as error:
        raise InputManifestError("input_artifact_not_ready") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InputManifestError("input_artifact_not_ready")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise InputManifestError("input_artifact_not_ready")
    except InputManifestError:
        raise
    except (OSError, TypeError) as error:
        raise InputManifestError("input_artifact_not_ready") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_input_files(workspace: Path) -> tuple[InputFile, ...]:
    """Parse the Worker manifest before importing Adapter code.

    The Worker owns the size/SHA-256 verification before process start.  The
    harness only repeats cheap path, existence, and controlled-file checks so
    those checks cannot consume the Adapter execution budget.
    """
    workspace_match = _WORKSPACE_NAME_PATTERN.fullmatch(workspace.name)
    if not workspace.is_absolute() or workspace_match is None:
        raise InputManifestError("input_artifact_not_ready")
    execution_id = int(workspace_match.group(1))
    try:
        input_info = (workspace / "input").lstat()
        manifest_path = workspace / "input_manifest.json"
        manifest_info = manifest_path.lstat()
    except OSError as error:
        raise InputManifestError("input_artifact_not_ready") from error
    if (
        stat.S_ISLNK(input_info.st_mode)
        or not stat.S_ISDIR(input_info.st_mode)
        or stat.S_ISLNK(manifest_info.st_mode)
        or not stat.S_ISREG(manifest_info.st_mode)
    ):
        raise InputManifestError("input_artifact_not_ready")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise InputManifestError("input_artifact_not_ready") from error
    if not isinstance(manifest, Mapping) or set(manifest) != {"execution_id", "files"}:
        raise InputManifestError("input_artifact_not_ready")
    if _manifest_int(manifest.get("execution_id"), positive=True) != execution_id:
        raise InputManifestError("input_artifact_not_ready")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > 8:
        raise InputManifestError("input_artifact_not_ready")
    result: list[InputFile] = []
    for expected_ordinal, raw_descriptor in enumerate(files):
        if not isinstance(raw_descriptor, Mapping) or set(raw_descriptor) != _MANIFEST_FIELDS:
            raise InputManifestError("input_artifact_not_ready")
        artifact_id = _manifest_int(raw_descriptor.get("artifact_id"), positive=True)
        ordinal = _manifest_int(raw_descriptor.get("ordinal"))
        mount_name = raw_descriptor.get("mount_name")
        original_name = raw_descriptor.get("original_filename")
        content_type = raw_descriptor.get("content_type")
        size_bytes = _manifest_int(raw_descriptor.get("size_bytes"))
        sha256 = raw_descriptor.get("sha256")
        mount_match = (
            _MOUNT_NAME_PATTERN.fullmatch(mount_name) if isinstance(mount_name, str) else None
        )
        if (
            artifact_id is None
            or ordinal != expected_ordinal
            or ordinal < 0
            or ordinal > 7
            or mount_match is None
            or int(mount_match.group(1)) != expected_ordinal
            or not isinstance(original_name, str)
            or not isinstance(content_type, str)
            or size_bytes is None
            or size_bytes < 0
            or not isinstance(sha256, str)
            or _SHA256_PATTERN.fullmatch(sha256) is None
        ):
            raise InputManifestError("input_artifact_not_ready")
        if not isinstance(mount_name, str):
            raise InputManifestError("input_artifact_not_ready")
        target = workspace / "input" / mount_name
        if target.parent != workspace / "input" or not target.is_absolute():
            raise InputManifestError("input_artifact_not_ready")
        _validate_input_file(target)
        result.append(
            InputFile(
                ordinal=expected_ordinal,
                path=target,
                original_name=original_name,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
            )
        )
    return tuple(result)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("dlr.adapter")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        # M5.5.10: no own timestamp (the Worker adds the unified per-line
        # timestamp at capture time); only the level marker stays.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def _load_adapter(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("dlr_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load adapter module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    workspace = Path(sys.argv[1])
    input_value = json.loads((workspace / "input.json").read_text(encoding="utf-8"))
    config = json.loads((workspace / "runtime_config.json").read_text(encoding="utf-8"))
    input_files = _load_input_files(workspace)

    module = _load_adapter(workspace / "adapter.py")
    context = Context(
        config=config,
        secrets=Secrets(),
        logger=_build_logger(),
        input_files=input_files,
    )
    result = module.handle(context, input_value)

    # A non-serializable return value raises here and becomes a failure.
    (workspace / "output.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InputManifestError as error:
        # Diagnostic only.  The Worker preflight owns the structured error code.
        print(f"DLR_INPUT_ERROR:{error.code}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
