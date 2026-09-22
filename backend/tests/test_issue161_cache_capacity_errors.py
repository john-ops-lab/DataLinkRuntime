"""Stable v3 capacity errors at each language cache-build entry point."""

from collections.abc import Callable
from pathlib import Path

import pytest

from dlr.worker import goenv, javaenv, nodeenv, venv
from dlr.worker.cache import CacheError

Prepare = Callable[[Path], Path]


def _typescript_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "node_modules" / "typescript"
    compiler = package / "bin" / "tsc"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("fixture", encoding="utf-8")
    (package / "package.json").write_text('{"version":"fixture"}', encoding="utf-8")
    node = tmp_path / "bin" / "node"
    node.parent.mkdir()
    node.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        nodeenv.shutil,
        "which",
        lambda command: str(compiler if command == "tsc" else node),
    )


def _preparer(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Prepare:
    if language == "python":
        return lambda root: venv.prepare_version_venv(
            root, 41, 51, "", timeout_seconds=5
        )
    if language in {"javascript", "typescript"}:
        if language == "typescript":
            _typescript_tools(tmp_path, monkeypatch)
        return lambda root: nodeenv.prepare_version_node(
            root,
            41,
            51,
            "export default async function(input) { return input; }",
            "",
            timeout_seconds=5,
            registry_url=None,
            language=language,
        )
    if language == "java":
        return lambda root: javaenv.prepare_version_java(
            root,
            41,
            51,
            "public class Adapter {}",
            "",
            timeout_seconds=5,
            repository_url=None,
        )
    if language == "go":
        compiler = tmp_path / "go"
        compiler.write_text("fixture", encoding="utf-8")
        monkeypatch.setattr(goenv.shutil, "which", lambda command: str(compiler))
        return lambda root: goenv.prepare_version_go(
            root,
            41,
            51,
            "package main",
            "",
            timeout_seconds=5,
            proxy_url=None,
        )
    raise AssertionError(language)


@pytest.mark.parametrize(
    "language", ["python", "javascript", "typescript", "java", "go"]
)
@pytest.mark.parametrize(
    ("entry", "error_code", "expected"),
    [
        ("normal", "cache_no_safe_candidates", "cache_no_safe_candidates"),
        ("normal", "cache_capacity_insufficient", "cache_capacity_insufficient"),
        ("normal", "cache_owner_mismatch", "dependency_preparation_failed"),
        ("replacement", "cache_no_safe_candidates", "cache_no_safe_candidates"),
        ("replacement", "cache_capacity_insufficient", "cache_capacity_insufficient"),
        ("replacement", "cache_owner_mismatch", "dependency_preparation_failed"),
    ],
)
def test_language_build_entries_preserve_only_stable_capacity_codes(
    language: str,
    entry: str,
    error_code: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _preparer(language, tmp_path, monkeypatch)
    calls: list[bool] = []
    empty = tmp_path / "empty-entry"
    empty.mkdir()

    def begin(*_args: object, **kwargs: object) -> tuple[object, Path, None]:
        replacement = kwargs.get("force_replacement") is True
        calls.append(replacement)
        if entry == "replacement" and not replacement:
            return object(), empty, None
        raise CacheError(error_code)

    monkeypatch.setattr(venv, "_begin_version_build", begin)
    with pytest.raises(venv.DependencyPreparationError) as caught:
        prepare(tmp_path / f"runtime-{language}-{entry}-{error_code}")

    assert caught.value.error_code == expected
    assert str(caught.value) == "version cache is unavailable"
    assert error_code not in str(caught.value)
    assert calls == ([False, True] if entry == "replacement" else [False])


def test_executor_result_contract_reads_dependency_error_code() -> None:
    source = (
        Path(venv.__file__).with_name("executor.py").read_text(encoding="utf-8")
    )
    assert '"error_code": preparation.error_code' in source
