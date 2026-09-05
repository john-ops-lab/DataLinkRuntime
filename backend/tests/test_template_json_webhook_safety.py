"""Focused cross-runtime safety contracts for the JSON and Webhook recipes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_template_recipes import (
    CATALOG_ROOT,
    _java_errors,
    _java_result,
    _javascript_errors,
    _javascript_result,
    _python_result,
)


def _sources(slug: str) -> dict[str, Path]:
    root = CATALOG_ROOT / f"variants/{slug}"
    return {
        "python": root / "python.py",
        "javascript": root / "javascript.mjs",
        "java": root / "java.java",
    }


def _results(slug: str, input_value: dict[str, object], tmp_path: Path) -> dict[str, object]:
    sources = _sources(slug)
    return {
        "python": _python_result(sources["python"], input_value),
        "javascript": _javascript_result(sources["javascript"], input_value),
        "java": _java_result(sources["java"], input_value, tmp_path),
    }


def test_webhook_paths_use_bounded_syntax_and_safe_error_locators(tmp_path: Path) -> None:
    missing = {
        "payload": {},
        "required": ["private.token"],
        "mappings": [
            {"source": "private.value", "target": "copy", "required": True},
            {"source": "value", "target": "constructor.polluted"},
            {"source": "value", "target": "copy", "required": "yes"},
        ],
    }
    results = _results("webhook-json-normalization", missing, tmp_path / "safe-fields")
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"] == {
        "valid": False,
        "data": None,
        "errors": [
            {"field": "required[0]", "code": "required"},
            {"field": "mappings[0].source", "code": "missing"},
            {"field": "mappings[1]", "code": "invalid_mapping"},
            {"field": "mappings[2]", "code": "invalid_mapping"},
        ],
        "partial": False,
    }
    assert "private" not in json.dumps(results, separators=(",", ":"))

    invalid_required = {"payload": {}, "required": ["x" * 257]}
    sources = _sources("webhook-json-normalization")
    with pytest.raises(ValueError, match="^invalid_required$"):
        _python_result(sources["python"], invalid_required)
    assert _javascript_errors(sources["javascript"], [invalid_required]) == ["invalid_required"]
    java_error = _java_errors(sources["java"], [invalid_required], tmp_path / "invalid-required")[0]
    assert java_error is not None and "invalid_required" in java_error
    assert "x" * 257 not in java_error


def test_webhook_complete_envelope_and_errors_stay_within_output_budget(tmp_path: Path) -> None:
    input_value = {
        "payload": {"blob": "x" * 100},
        "required": [f"missing{index}" for index in range(20)],
        "mappings": [{"source": "blob", "target": "copy"}],
        "max_output_bytes": 128,
    }
    results = _results("webhook-json-normalization", input_value, tmp_path / "budget")
    assert results["python"] == results["javascript"] == results["java"]
    result = results["python"]
    assert isinstance(result, dict) and result["partial"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= 128
    assert "missing" not in json.dumps(result, separators=(",", ":"))

    output_limited = _results(
        "webhook-json-normalization",
        {
            "payload": {"blob": "x" * 100},
            "mappings": [{"source": "blob", "target": "copy"}],
            "max_output_bytes": 128,
        },
        tmp_path / "data-budget",
    )
    assert output_limited["python"] == output_limited["javascript"] == output_limited["java"]
    assert output_limited["python"] == {
        "valid": False,
        "data": None,
        "errors": [{"field": "", "code": "output_limit"}],
        "partial": False,
    }


def test_json_filter_equality_and_canonical_object_sort_match_all_runtimes(
    tmp_path: Path,
) -> None:
    strict = {
        "records": [
            {"id": "boolean", "candidate": {"value": True}},
            {"id": "number", "candidate": {"value": 1}},
        ],
        "mappings": [{"pointer": "/id", "target": "id"}],
        "filters": [{"pointer": "/candidate", "op": "equals", "value": {"value": True}}],
    }
    strict_results = _results("json-mapping-cleaning", strict, tmp_path / "strict-equality")
    assert strict_results["python"] == strict_results["javascript"] == strict_results["java"]
    assert strict_results["python"]["records"] == [{"id": "boolean"}]

    canonical = {
        "records": [
            {"id": "first", "order": {"b": 1, "a": [{"d": 4, "c": 3}]}},
            {"id": "second", "order": {"a": [{"c": 3, "d": 4}], "b": 1}},
        ],
        "mappings": [
            {"pointer": "/id", "target": "id"},
            {"pointer": "/order", "target": "order"},
        ],
        "sort": {"field": "order", "direction": "asc"},
    }
    canonical_results = _results("json-mapping-cleaning", canonical, tmp_path / "canonical")
    assert (
        canonical_results["python"] == canonical_results["javascript"] == canonical_results["java"]
    )
    assert [item["id"] for item in canonical_results["python"]["records"]] == [
        "first",
        "second",
    ]


def test_json_conversion_failure_does_not_echo_target(tmp_path: Path) -> None:
    target = "private_target_name"
    input_value = {
        "records": [{"value": "not-an-integer"}],
        "mappings": [{"pointer": "/value", "target": target, "type": "integer"}],
    }
    sources = _sources("json-mapping-cleaning")
    with pytest.raises(ValueError) as python_error:
        _python_result(sources["python"], input_value)
    assert str(python_error.value) == "conversion_failed"
    assert _javascript_errors(sources["javascript"], [input_value]) == ["conversion_failed"]
    java_error = _java_errors(sources["java"], [input_value], tmp_path / "conversion")[0]
    assert java_error is not None and "conversion_failed" in java_error
    assert target not in java_error
