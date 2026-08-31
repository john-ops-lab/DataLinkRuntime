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
