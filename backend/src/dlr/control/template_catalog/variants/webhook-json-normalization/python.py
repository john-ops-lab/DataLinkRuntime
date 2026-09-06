"""Pure webhook payload validation and normalization."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

# Webhook 数据整理：可修改的配置集中在这里。
# 运行时提供待处理的数据或文件；处理规则在下面配置。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
CONFIG = {
    # 必须存在的 Webhook 字段。
    "required": ["event_id"],
    # 字段映射规则；source/pointer 指定原字段路径，target 指定结果字段名。
    "mappings": [
        {"source": "event_id", "target": "id", "required": True},
        {"source": "occurred_at", "target": "timestamp", "type": "datetime", "required": True},
    ],
    # 最多处理的字段数。
    "max_fields": 200,
    # 输入大小上限，单位字节。
    "max_input_bytes": 1048576,
    # 返回结果大小上限，单位字节。
    "max_output_bytes": 2097152,
    # JSON 最大嵌套层数。
    "max_depth": 32,
}


_DANGEROUS_TARGET_SEGMENTS = {"__proto__", "prototype", "constructor"}
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_PATH_LENGTH = 256
_MAX_PATH_SEGMENTS = 32
_MIN_OUTPUT_BYTES = 128


def _valid_path(path: object, *, target: bool = False) -> bool:
    if not isinstance(path, str) or not 1 <= len(path) <= _MAX_PATH_LENGTH:
        return False
    segments = path.split(".")
    return (
        len(segments) <= _MAX_PATH_SEGMENTS
        and all(_PATH_SEGMENT.fullmatch(segment) for segment in segments)
        and (not target or not any(segment in _DANGEROUS_TARGET_SEGMENTS for segment in segments))
    )


def _read_path(value: object, path: str) -> object:
    current = value
    for segment in path.split("."):
        if not segment:
            raise ValueError("invalid_path")
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            raise KeyError(path)
    return current


def _utc(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp_must_be_string")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp_requires_timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _assign(target: dict[str, object], path: str, value: object) -> None:
    segments = path.split(".")
    if not _valid_path(path, target=True):
        raise ValueError("invalid_target_path")
    current = target
    for segment in segments[:-1]:
        child = current.setdefault(segment, {})
        if not isinstance(child, dict):
            raise ValueError("target_path_conflict")
        current = child
    current[segments[-1]] = value


def _exceeds_depth(value: object, maximum: int) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        current_depth = parent_depth + 1
        if current_depth > maximum:
            return True
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, current_depth) for child in children)
    return False


def _serialized_size(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise ValueError("payload_too_large") from None


def _bounded_result(
    errors: list[dict[str, str]],
    normalized: dict[str, object],
    max_fields: int,
    max_output_bytes: int,
) -> dict[str, object]:
    if not errors:
        result: dict[str, object] = {
            "valid": True,
            "data": normalized,
            "errors": [],
            "partial": False,
        }
        if _serialized_size(result) <= max_output_bytes:
            return result
        errors = [{"field": "", "code": "output_limit"}]

    selected: list[dict[str, str]] = []
    for error in errors[:max_fields]:
        candidate = {
            "valid": False,
            "data": None,
            "errors": [*selected, error],
            "partial": True,
        }
        if _serialized_size(candidate) > max_output_bytes:
            break
        selected.append(error)

    partial = len(selected) < len(errors)
    result = {
        "valid": False,
        "data": None,
        "errors": selected,
        "partial": partial,
    }
    if not partial and _serialized_size(result) > max_output_bytes:
        selected.pop()
        result = {
            "valid": False,
            "data": None,
            "errors": selected,
            "partial": True,
        }
    return result


def handle(context, input):
    if input is None:
        input = {}
    if not isinstance(input, dict):
        raise ValueError("输入必须是 JSON 对象")
    input = {**CONFIG, **input}
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    payload = input.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload_must_be_object")
    required = input.get("required", [])
    mappings = input.get("mappings", [])
    max_fields = input.get("max_fields", 200)
    max_input_bytes = input.get("max_input_bytes", 1_048_576)
    max_output_bytes = input.get("max_output_bytes", 2_097_152)
    max_depth = input.get("max_depth", 32)
    if (
        not isinstance(max_fields, int)
        or isinstance(max_fields, bool)
        or not 1 <= max_fields <= 1000
    ):
        raise ValueError("invalid_max_fields")
    for value, maximum, code in (
        (max_input_bytes, 8_388_608, "invalid_max_input_bytes"),
        (max_output_bytes, 8_388_608, "invalid_max_output_bytes"),
        (max_depth, 64, "invalid_max_depth"),
    ):
        minimum = _MIN_OUTPUT_BYTES if code == "invalid_max_output_bytes" else 1
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ValueError(code)
    if _exceeds_depth(payload, max_depth):
        raise ValueError("payload_too_deep")
    if _serialized_size(payload) > max_input_bytes:
        raise ValueError("payload_too_large")
    if (
        not isinstance(required, list)
        or len(required) > max_fields
        or not all(_valid_path(path) for path in required)
    ):
        raise ValueError("invalid_required")
    if not isinstance(mappings, list) or len(mappings) > max_fields:
        raise ValueError("invalid_mappings")
    errors: list[dict[str, str]] = []
    for index, path in enumerate(required):
        try:
            value = _read_path(payload, path)
            if value is None:
                raise KeyError(path)
        except (KeyError, ValueError):
            errors.append({"field": f"required[{index}]", "code": "required"})
    normalized: dict[str, object] = {}
    normalized_budget = 2
    assigned_mappings = 0
    valid_result_overhead = (
        _serialized_size({"valid": True, "data": {}, "errors": [], "partial": False}) - 2
    )
    for index, mapping in enumerate(mappings):
        field = f"mappings[{index}]"
        if not isinstance(mapping, dict):
            errors.append({"field": field, "code": "invalid_mapping"})
            continue
        source = mapping.get("source")
        target = mapping.get("target")
        mapping_required = mapping.get("required", False)
        conversion = mapping.get("type")
        if (
            not _valid_path(source)
            or not _valid_path(target, target=True)
            or not isinstance(mapping_required, bool)
            or conversion not in (None, "datetime")
        ):
            errors.append({"field": field, "code": "invalid_mapping"})
            continue
        try:
            value = _read_path(payload, source)
        except KeyError:
            if "default" in mapping:
                value = mapping["default"]
            elif mapping_required:
                errors.append({"field": f"{field}.source", "code": "missing"})
                continue
            else:
                continue
        except (TypeError, ValueError):
            errors.append({"field": field, "code": "invalid_value"})
            continue
        try:
            if conversion == "datetime":
                value = _utc(value)
            candidate: dict[str, object] = {}
            _assign(candidate, target, value)
            addition = _serialized_size(candidate) - 2 + (1 if assigned_mappings else 0)
            if valid_result_overhead + normalized_budget + addition > max_output_bytes:
                errors.append({"field": "", "code": "output_limit"})
                break
            _assign(normalized, target, value)
        except (TypeError, ValueError):
            errors.append({"field": field, "code": "invalid_value"})
            continue
        normalized_budget += addition
        assigned_mappings += 1
    return _bounded_result(errors, normalized, max_fields, max_output_bytes)
