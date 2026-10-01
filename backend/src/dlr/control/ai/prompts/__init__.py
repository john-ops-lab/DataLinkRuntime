"""Versioned, package-owned static instructions for the AI assistant."""

from hashlib import sha256
from importlib import resources
from types import MappingProxyType
from typing import Final

_NAMES: Final = ("system.md", "adapter.md", "tools.md")


def _load_resources() -> tuple[dict[str, str], str]:
    rules: dict[str, str] = {}
    for name in _NAMES:
        try:
            raw = resources.files(__package__).joinpath(name).read_bytes()
            content = raw.decode("utf-8", errors="strict")
        except (FileNotFoundError, UnicodeError) as error:
            raise RuntimeError(f"AI prompt resource invalid: {name}") from error
        if not content.strip():
            raise RuntimeError(f"AI prompt resource empty: {name}")
        rules[name] = content
    revision = sha256(
        b"".join(name.encode() + b"\0" + rules[name].encode() + b"\0" for name in _NAMES)
    ).hexdigest()
    return rules, revision


_RULES, REVISION = _load_resources()
RULES = MappingProxyType(_RULES)
