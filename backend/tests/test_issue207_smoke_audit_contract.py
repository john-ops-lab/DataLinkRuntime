"""Exercise the actual smoke audit validator without running Compose side effects."""

import ast
from pathlib import Path
from typing import Any

import pytest


def _contract() -> dict[str, Any]:
    source = Path(__file__).resolve().parents[2] / "scripts" / "compose-smoke.py"
    tree = ast.parse(source.read_text())
    names = {
        "audit_common_fields",
        "audit_event_fields",
        "audit_identity_fields",
        "budget_counters",
        "failure_stages",
        "failure_reasons",
        "validate_provider_audit",
        "validate_correction_audit",
    }
    nodes = []
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names
            or isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
            or (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and isinstance(node.value.func.value, ast.Name)
                and node.value.func.value.id == "audit_event_fields"
            )
        ):
            nodes.append(node)
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def _record(event: str, strategy: str = "prompt_only", count: int = 0) -> dict[str, Any]:
    record = {
        "timestamp": "2026-10-14T00:00:00Z",
        "schema_version": 1,
        "event_type": event,
        "request_id": "request",
        "conversation_id": "conversation",
        "adapter_id": 1,
        "provider_call_index": 2,
        "strategy": strategy,
        "correction_count": count,
    }
    if event == "format_correction":
        record["elapsed_ms"] = 10
    else:
        record.update(
            outcome="final",
            duration_ms=1,
            remaining_ms=100,
            stage=None,
            reason=None,
            error_code=None,
        )
    return record


@pytest.mark.parametrize("strategy", ["prompt_only", "json_object", "json_schema"])
def test_current_provider_strategies_and_once_correction(strategy: str) -> None:
    contract = _contract()
    correction = _record("format_correction", strategy, 1)
    result = _record("provider_result", strategy, 1)
    for record in [correction, result, _record("provider_result", strategy, 0)]:
        contract["validate_provider_audit"](record)
    contract["validate_correction_audit"]([correction, result])


@pytest.mark.parametrize(
    "event,field,value",
    [
        ("provider_result", "strategy", "arbitrary"),
        ("format_correction", "strategy", None),
        ("provider_result", "correction_count", True),
        ("provider_result", "correction_count", 2),
        ("format_correction", "correction_count", 0),
        ("format_correction", "correction_count", 2),
        ("format_correction", "elapsed_ms", -1),
        ("format_correction", "elapsed_ms", True),
        ("provider_result", "response", "PRIVATE RAW RESPONSE"),
    ],
)
def test_audit_contract_rejects_invalid_fields(event: str, field: str, value: object) -> None:
    record = _record(event, count=1 if event == "format_correction" else 0)
    record[field] = value
    with pytest.raises(AssertionError):
        _contract()["validate_provider_audit"](record)


def test_correction_cannot_repeat_or_claim_another_strategy() -> None:
    contract = _contract()
    correction = _record("format_correction", count=1)
    with pytest.raises(AssertionError):
        contract["validate_correction_audit"]([correction, correction])
    with pytest.raises(AssertionError):
        contract["validate_correction_audit"]([_record("provider_result", count=1)])
    with pytest.raises(AssertionError):
        contract["validate_correction_audit"](
            [correction, _record("provider_result", "json_object", 1)]
        )
