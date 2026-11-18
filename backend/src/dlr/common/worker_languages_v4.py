"""Closed language capabilities for the restricted fixed Worker stage."""

from typing import Final

NODE_EXECUTABLE: Final = "/usr/local/bin/node"
JAVA_EXECUTABLE: Final = "/opt/java/openjdk/bin/java"
JAVA_COMPILER: Final = "/opt/java/openjdk/bin/javac"
TYPESCRIPT_ROOT: Final = "/opt/dlr-typescript/node_modules"
TYPESCRIPT_COMPILER: Final = TYPESCRIPT_ROOT + "/typescript/lib/tsc.js"
COMPILED_LANGUAGES: Final = frozenset({"typescript", "java", "go"})
GO_ARTIFACT: Final = ".dlr-go-artifact"
GO_ARTIFACT_DIGEST: Final = ".dlr-go-artifact.sha256"
_LANGUAGE_MODES: Final = {
    "python": ("python",),
    "python_javascript": ("python", "javascript"),
    "python_javascript_typescript_java": ("python", "javascript", "typescript", "java"),
    "python_javascript_typescript_java_go": ("python", "javascript", "typescript", "java", "go"),
}
SUPPORTED_LANGUAGES: Final = frozenset(_LANGUAGE_MODES["python_javascript_typescript_java_go"])
WORKSPACE_LANGUAGE_FILES: Final = {
    "python": frozenset({".dlr-harness.py", "adapter.py"}),
    "javascript": frozenset({".dlr-harness.mjs", "adapter.mjs"}),
    "typescript": frozenset({".dlr-harness.mjs", "adapter.mts", "dlr.d.ts"}),
    "java": frozenset({"Adapter.java", "DlrRuntime.java"}),
    "go": frozenset({"adapter.go", "dlr_runtime.go", GO_ARTIFACT, GO_ARTIFACT_DIGEST}),
}


def valid_capabilities(value: object) -> bool:
    return type(value) is list and any(value == list(mode) for mode in _LANGUAGE_MODES.values())


def valid_language_mode(value: object) -> bool:
    return type(value) is str and value in _LANGUAGE_MODES


def mode_capabilities(mode: str) -> list[str]:
    try:
        return list(_LANGUAGE_MODES[mode])
    except KeyError:
        raise ValueError("Worker language mode unavailable") from None
