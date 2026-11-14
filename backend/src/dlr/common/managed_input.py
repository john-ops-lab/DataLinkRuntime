"""Shared Managed Input filename contract.

Only Control decides which user-visible suffixes may enter the system. Worker
and runtime code validate the resulting opaque mount-name grammar, but never
maintain their own product allowlist.
"""

from __future__ import annotations

import re

MANAGED_INPUT_FILE_EXTENSIONS = (
    ".xlsx",
    ".xls",
    ".csv",
    ".log",
    ".txt",
    ".json",
)
MANAGED_INPUT_FILE_EXTENSION_SET = frozenset(MANAGED_INPUT_FILE_EXTENSIONS)
MANAGED_INPUT_FILE_EXTENSION_ALTERNATION = "|".join(
    re.escape(extension.removeprefix(".")) for extension in MANAGED_INPUT_FILE_EXTENSIONS
)


def controlled_input_mount_name(ordinal: int, original_filename: str) -> str:
    """Keep only the supported suffix in a deterministic, path-free name."""
    suffix = (
        f".{original_filename.rsplit('.', 1)[-1].casefold()}" if "." in original_filename else ""
    )
    if suffix not in MANAGED_INPUT_FILE_EXTENSION_SET:
        suffix = ""
    return f"input-{ordinal:02d}{suffix}"
