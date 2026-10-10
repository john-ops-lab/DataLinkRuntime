from pathlib import Path

import pytest

from test_multilang_runtime import payload, runtime_settings
from worker_runtime_support import run_with_test_sandbox


@pytest.mark.parametrize("language", ["java", "go"])
@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_invalid_declaration_reports_line_and_syntax_without_raw_value(
    tmp_path: Path, language: str, locale: str
) -> None:
    value = payload(language, "not reached")
    value.update(requirements="# comment\n\nSYNTHETIC-SECRET-NOT-A-DECLARATION", locale=locale)
    result = run_with_test_sandbox(value, runtime_settings(tmp_path))
    assert result["error_code"] == "dependency_declaration_invalid"
    assert result["workspace_cleanup_status"] == "completed"
    text = result["error"] + result["stdout"]
    assert ("第 3 行" if locale == "zh-CN" else "line 3") in text
    assert ("groupId:artifactId:version" if language == "java" else "module/path@vX.Y.Z") in text
    assert "SYNTHETIC-SECRET-NOT-A-DECLARATION" not in text
