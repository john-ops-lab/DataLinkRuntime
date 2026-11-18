"""Bounded frozen pure-Python package contract for a fixed Worker."""

import email
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from dlr.common.builtin_packages import safe_path
from dlr.common.managed_files_v4 import MAX_FILE_BYTES, InputFile

_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,79})==([A-Za-z0-9][A-Za-z0-9_.+-]{0,63})")
_FILENAME = re.compile(r"[A-Za-z0-9_+.]+-[A-Za-z0-9_+.]+-py3-none-any\.whl")
_DIGEST = re.compile(r"[0-9a-f]{64}")
SITE_DIRECTORY = "dlr-builtin-site"
CAPABILITY = "builtin_wheel_v1"
MOUNT_NAME = "input-00.whl"
MAX_EXPANDED_BYTES = 4 * MAX_FILE_BYTES
MAX_WHEEL_ENTRIES = 512
PREFLIGHT_CODE = (
    "import zipfile, sys\n"
    "from dlr.common.builtin_wheel_v4 import unpack\n"
    "wheel=pathlib.Path('preflight-wheel.whl'); dest=pathlib.Path('preflight-wheel')\n"
    "with zipfile.ZipFile(wheel, 'w') as z:\n"
    "    z.writestr('dlr_builtin_preflight.py', 'value = 42\\n')\n"
    "    z.writestr('dlr_preflight-1.0.dist-info/WHEEL', "
    "'Root-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
    "unpack(wheel,dest)\n"
    "sys.path.insert(0,str(dest.resolve()))\n"
    "import dlr_builtin_preflight\n"
    "builtin_probe=dlr_builtin_preflight.value==42\n"
)


def requirement(value: object) -> tuple[str, str] | None:
    if type(value) is not str or len(value) > 160:
        return None
    match = _REQUIREMENT.fullmatch(value.strip())
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match[1]).lower(), match[2]


def descriptor(snapshot: object, requirements: object) -> InputFile:
    """Read the trusted worker snapshot without accepting package index options."""
    requested = requirement(requirements)
    if (
        requested is None
        or type(snapshot) is not dict
        or set(snapshot) != {"kind", "files"}
        or snapshot["kind"] != "pypi"
        or type(snapshot["files"]) is not list
        or len(snapshot["files"]) != 1
    ):
        raise ValueError("builtin wheel unavailable")
    row: Any = snapshot["files"][0]
    if type(row) is not dict or set(row) != {
        "id",
        "filename",
        "repository_path",
        "size_bytes",
        "sha256",
        "metadata",
    }:
        raise ValueError("builtin wheel unavailable")
    name, version = requested
    filename = row["filename"]
    if (
        type(filename) is not str
        or len(filename) > 192
        or _FILENAME.fullmatch(filename) is None
        or row["repository_path"] != filename
        or re.sub(r"[-_.]+", "-", filename.split("-")[0]).lower() != name
        or filename.split("-")[1] != version
        or type(row["id"]) is not int
        or not 0 < row["id"] < 2**63
        or type(row["size_bytes"]) is not int
        or not 0 < row["size_bytes"] <= MAX_FILE_BYTES
        or type(row["sha256"]) is not str
        or _DIGEST.fullmatch(row["sha256"]) is None
        or row["metadata"] != {"dependencies": [], "requires_python": None}
    ):
        raise ValueError("builtin wheel unavailable")
    return InputFile(row["id"], 0, MOUNT_NAME, row["size_bytes"], row["sha256"])


def members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Inspect a small wheel for safe in-sandbox extraction, without importing it."""
    entries = archive.infolist()
    if not 1 <= len(entries) <= MAX_WHEEL_ENTRIES:
        raise ValueError("builtin wheel entries unavailable")
    names: set[PurePosixPath] = set()
    result: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    wheel: zipfile.ZipInfo | None = None
    total = 0
    for entry in entries:
        path = PurePosixPath(safe_path(entry.filename.rstrip("/"), archive_member=True))
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            path in names
            or len(path.parts) > 12
            or path.parts[0].endswith(".data")
            or path.suffix.lower() in {".pth", ".so", ".pyd", ".dll", ".dylib"}
            or mode not in {0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG}
            or entry.flag_bits & 1
        ):
            raise ValueError("builtin wheel entry unavailable")
        names.add(path)
        total += entry.file_size
        if total > MAX_EXPANDED_BYTES:
            raise ValueError("builtin wheel expansion unavailable")
        if len(path.parts) == 2 and path.parts[0].endswith(".dist-info") and path.name == "WHEEL":
            if wheel is not None or entry.is_dir() or entry.file_size > 4096:
                raise ValueError("builtin wheel metadata unavailable")
            wheel = entry
        result.append((entry, path))
    if wheel is None:
        raise ValueError("builtin wheel metadata unavailable")
    metadata = email.message_from_bytes(archive.read(wheel))
    if metadata.get_all("Root-Is-Purelib") != ["true"] or metadata.get_all("Tag") != [
        "py3-none-any"
    ]:
        raise ValueError("builtin wheel ABI unavailable")
    return result


def unpack(path: Path, destination: Path) -> None:
    """Publish the inspected archive inside the task-owned sandbox tmpfs."""
    with zipfile.ZipFile(path) as archive:
        entries = members(archive)
        destination.mkdir(mode=0o700)
        for entry, name in entries:
            target = destination.joinpath(*name.parts)
            if entry.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("xb") as stream:
                remaining = entry.file_size
                while remaining:
                    block = source.read(min(64 * 1024, remaining))
                    if not block:
                        raise ValueError("builtin wheel file incomplete")
                    stream.write(block)
                    remaining -= len(block)
                if source.read(1):
                    raise ValueError("builtin wheel file exceeds its descriptor")
