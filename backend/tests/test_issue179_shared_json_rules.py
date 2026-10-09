from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_template_recipes import CATALOG_ROOT, _java_result, _javascript_result, _python_result

CASES = json.loads((Path(__file__).parent / "fixtures/issue179/shared-cases.json").read_text())


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
@pytest.mark.parametrize("case", CASES, ids=[x["name"] for x in CASES])
def test_shared_recipe_uses_one_full_json_oracle(language: str, case: dict, tmp_path: Path) -> None:
    extension = {"python": "py", "javascript": "mjs", "java": "java"}[language]
    source = CATALOG_ROOT / "variants/json-mapping-cleaning" / f"{language}.{extension}"
    if language == "python":
        result = _python_result(source, case["input"])
    elif language == "javascript":
        result = _javascript_result(source, case["input"])
    else:
        result = _java_result(source, case["input"], tmp_path / "java")
    assert result == case["expected"]
