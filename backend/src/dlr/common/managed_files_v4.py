"""Bounded metadata for the fixed Worker's managed input capability.

This validates descriptors, never file contents or storage paths. The shared
Managed Input module remains the only authority for controlled file names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dlr.common.managed_input import controlled_input_mount_name

CAPABILITY = "managed_files_v1"
MAX_FILES = 4
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * MAX_FILE_BYTES
CHUNK_BYTES = 64 * 1024
FILE_TIMEOUT_SECONDS = 5.0
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset(
    {"id", "ordinal", "mount_name", "original_filename", "content_type", "size_bytes", "sha256"}
)


class ManagedFilesError(ValueError):
    """Only a stable, non-sensitive reason crosses the protocol boundary."""

    def __init__(self, reason: str = "input_files_snapshot") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class InputFile:
    artifact_id: int
    ordinal: int
    mount_name: str
    size_bytes: int
    sha256: str


def descriptors(value: object) -> tuple[InputFile, ...]:
    """Validate a complete, ordered snapshot before any file side effect."""
    if type(value) is not list or not 1 <= len(value) <= MAX_FILES:
        raise ManagedFilesError("input_files_budget")
    result = []
    seen: set[int] = set()
    total = 0
    for ordinal, item in enumerate(value):
        if type(item) is not dict or set(item) != _FIELDS:
            raise ManagedFilesError()
        artifact, size, name, digest = (
            item["id"],
            item["size_bytes"],
            item["original_filename"],
            item["sha256"],
        )
        try:
            name_bytes = len(name.encode("utf-8")) if type(name) is str else -1
        except UnicodeError:
            raise ManagedFilesError() from None
        if (
            type(artifact) is not int
            or not 0 < artifact < 2**63
            or artifact in seen
            or type(item["ordinal"]) is not int
            or item["ordinal"] != ordinal
            or type(size) is not int
            or size < 0
            or type(name) is not str
            or not 0 < len(name) <= 512
            or not 0 <= name_bytes <= 2048
            or type(item["content_type"]) is not str
            or len(item["content_type"]) > 255
            or type(digest) is not str
            or _HASH.fullmatch(digest) is None
            or item["mount_name"] != controlled_input_mount_name(ordinal, name)
        ):
            raise ManagedFilesError()
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ManagedFilesError("input_files_budget")
        seen.add(artifact)
        result.append(InputFile(artifact, ordinal, item["mount_name"], size, digest))
    return tuple(result)
