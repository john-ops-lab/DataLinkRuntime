"""Closed language capabilities for the restricted fixed Worker stage."""

from typing import Final

NODE_EXECUTABLE: Final = "/usr/local/bin/node"


def valid_capabilities(value: object) -> bool:
    return type(value) is list and value in (["python"], ["python", "javascript"])


def mode_capabilities(mode: str) -> list[str]:
    if mode == "python":
        return ["python"]
    if mode == "python_javascript":
        return ["python", "javascript"]
    raise ValueError("Worker language mode unavailable")
