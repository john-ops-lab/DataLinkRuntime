"""Finite RFC 6901 mapping, filtering, sorting and de-duplication."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime

# JSON 字段整理：可修改的配置集中在这里。
# 运行时提供待处理的数据或文件；处理规则在下面配置。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
CONFIG = {
    # 字段映射规则；source/pointer 指定原字段路径，target 指定结果字段名。
    "mappings": [
        {"pointer": "/profile/name", "target": "name", "type": "string", "default": ""},
        {"pointer": "/id", "target": "id", "type": "string"},
    ],
    # 按字段值筛选；空列表表示不过滤。
    "filters": [],
    # 排序字段和方向：asc 升序、desc 降序。
    "sort": {"field": "name", "direction": "asc"},
    # 按此字段去重。
    "dedupe_by": "id",
    # 单次运行最多返回的记录数。
    "max_records": 10000,
    # 最多处理的字段数。
    "max_fields": 200,
    # 返回结果大小上限，单位字节。
    "max_output_bytes": 4194304,
}


_INTEGER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_NUMBER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_POINTER_BYTES = 1_024
_MAX_POINTER_TOKENS = 64
_MAX_POINTER_TOKEN_BYTES = 256
_MAX_TARGET_BYTES = 256
_CONVERSIONS = {None, "string", "integer", "number", "boolean", "datetime"}


def _valid_pointer(pointer: object) -> bool:
    if not isinstance(pointer, str) or len(pointer.encode()) > _MAX_POINTER_BYTES:
        return False
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    tokens = pointer[1:].split("/")
    return len(tokens) <= _MAX_POINTER_TOKENS and all(
        not re.search(r"~(?:[^01]|$)", raw)
        and len(raw.replace("~1", "/").replace("~0", "~").encode()) <= _MAX_POINTER_TOKEN_BYTES
        for raw in tokens
    )


def _valid_target(target: object) -> bool:
    return (
        isinstance(target, str)
        and 1 <= len(target.encode()) <= _MAX_TARGET_BYTES
        and not any(ord(character) < 0x20 for character in target)
    )


def _integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_integer")
    if (
        isinstance(value, int)
        or isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
        or isinstance(value, str)
        and len(value) <= 32
        and _INTEGER_TEXT.fullmatch(value)
    ):
        converted = int(value)
    else:
        raise ValueError("invalid_integer")
    if abs(converted) > _MAX_SAFE_INTEGER:
        raise ValueError("invalid_integer")
    return converted


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid_number")
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and len(value) <= 128 and _NUMBER_TEXT.fullmatch(value)
    ):
        converted = float(value)
    else:
        raise ValueError("invalid_number")
    if not math.isfinite(converted):
        raise ValueError("invalid_number")
    return converted


def _pointer(value: object, pointer: str) -> object:
    if not _valid_pointer(pointer):
        raise ValueError("invalid_json_pointer")
    if pointer == "":
        return value
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise KeyError(pointer)
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise KeyError(pointer)
    return current


def _convert(value: object, kind: str | None) -> object:
    if kind is None:
        return value
    if kind == "string":
        return str(value)
    if kind == "integer":
        return _integer(value)
    if kind == "number":
        return _number(value)
    if kind == "boolean":
        if value in (True, "true", "1", 1):
            return True
        if value in (False, "false", "0", 0):
            return False
        raise ValueError("invalid_boolean")
    if kind == "datetime":
        if not isinstance(value, str):
            raise ValueError("invalid_datetime")
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError("datetime_requires_timezone")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    raise ValueError("unsupported_conversion")


def _matches(item: object, rule: dict[str, object]) -> bool:
    pointer = rule.get("pointer")
    if not isinstance(pointer, str):
        raise ValueError("invalid_filter")
    try:
        value = _pointer(item, pointer)
        exists = True
    except KeyError:
        value = None
        exists = False
    operator = rule.get("op", "equals")
    if operator == "exists":
        return exists is rule.get("value", True)
    if operator == "equals":
        return exists and _json_equals(value, rule.get("value"))
    raise ValueError("unsupported_filter")


def _sort_key(value: object) -> tuple[int, str]:
    if value is None:
        return (1, "")
    return (0, _canonical_json(value))


def _json_equals(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
            and left == right
        )
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_equals(a, b) for a, b in zip(left, right, strict=True))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_json_equals(left[key], right[key]) for key in left)
        )
    return type(left) is type(right) and left == right


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise ValueError("output_not_json") from None


def _json_size(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    except (OverflowError, RecursionError, TypeError, ValueError):
        raise ValueError("output_not_json") from None


def handle(context, input):
    if input is None:
        input = {}
    if not isinstance(input, dict):
        raise ValueError("输入必须是 JSON 对象")
    input = {**CONFIG, **input}
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    records = input.get("records")
    mappings = input.get("mappings")
    if not isinstance(records, list) or not isinstance(mappings, list):
        raise ValueError("records_and_mappings_required")
    max_records = input.get("max_records", 10_000)
    max_fields = input.get("max_fields", 200)
    max_output_bytes = input.get("max_output_bytes", 4_194_304)
    if (
        not isinstance(max_records, int)
        or isinstance(max_records, bool)
        or not 1 <= max_records <= 100_000
    ):
        raise ValueError("invalid_max_records")
    if (
        not isinstance(max_fields, int)
        or isinstance(max_fields, bool)
        or not 1 <= max_fields <= 1_000
        or len(mappings) > max_fields
    ):
        raise ValueError("invalid_max_fields")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or not 1 <= max_output_bytes <= 16_777_216
    ):
        raise ValueError("invalid_max_output_bytes")
    filters = input.get("filters", [])
    if not isinstance(filters, list):
        raise ValueError("invalid_filters")
    if len(filters) > max_fields or any(
        not isinstance(rule, dict)
        or not _valid_pointer(rule.get("pointer"))
        or rule.get("op", "equals") not in ("equals", "exists")
        or rule.get("op", "equals") == "exists"
        and "value" in rule
        and not isinstance(rule["value"], bool)
        for rule in filters
    ):
        raise ValueError("invalid_filter")
    dedupe = input.get("dedupe_by")
    if dedupe is not None and not _valid_target(dedupe):
        raise ValueError("invalid_dedupe")
    sort = input.get("sort")
    if sort is not None and (
        not isinstance(sort, dict)
        or not _valid_target(sort.get("field"))
        or sort.get("direction", "asc") not in ("asc", "desc")
    ):
        raise ValueError("invalid_sort")
    sort_field = sort["field"] if sort is not None else None
    descending = sort is not None and sort.get("direction", "asc") == "desc"
    seen: set[str] = set()
    candidates: list[tuple[tuple[int, str], int, dict[str, object] | None, int]] = []
    output_limited = False
    output_bytes = 2
    for ordinal, record in enumerate(records[:max_records]):
        if not all(isinstance(rule, dict) and _matches(record, rule) for rule in filters):
            continue
        output: dict[str, object] = {}
        field_sizes: dict[str, int] = {}
        object_bytes = 2
        record_limited = False
        sort_value: object = None
        dedupe_value: object = None
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ValueError("invalid_mapping")
            pointer = mapping.get("pointer")
            target = mapping.get("target")
            if (
                not isinstance(pointer, str)
                or not _valid_target(target)
                or mapping.get("type") not in _CONVERSIONS
            ):
                raise ValueError("invalid_mapping")
            if not _valid_pointer(pointer):
                raise ValueError("invalid_json_pointer")
            try:
                value = _pointer(record, pointer)
            except KeyError:
                if "default" not in mapping:
                    continue
                value = mapping["default"]
            try:
                converted = _convert(value, mapping.get("type"))
                if target == sort_field:
                    sort_value = converted
                if target == dedupe:
                    dedupe_value = converted
                if record_limited:
                    continue
                field_size = _json_size(target) + 1 + _json_size(converted)
            except (TypeError, ValueError):
                raise ValueError("conversion_failed") from None
            previous_size = field_sizes.get(target)
            next_object_bytes = object_bytes - (previous_size or 0) + field_size
            if previous_size is None and field_sizes:
                next_object_bytes += 1
            if next_object_bytes > max_output_bytes:
                record_limited = True
                if sort_field is None:
                    break
                continue
            output[target] = converted
            field_sizes[target] = field_size
            object_bytes = next_object_bytes
        if dedupe is not None:
            dedupe_key = _canonical_json(dedupe_value if record_limited else output.get(dedupe))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
        if record_limited:
            output_limited = True
            if sort_field is None:
                break
        candidate_output = None if record_limited else output
        encoded_bytes = max_output_bytes + 1 if record_limited else object_bytes
        if sort_field is None:
            addition = encoded_bytes + (1 if candidates else 0)
            if output_bytes + addition > max_output_bytes:
                output_limited = True
                break
            candidates.append(((0, ""), ordinal, output, encoded_bytes))
            output_bytes += addition
            continue

        order = _sort_key(sort_value if record_limited else output.get(sort_field))
        low = 0
        high = len(candidates)
        while low < high:
            middle = (low + high) // 2
            middle_order, middle_ordinal, _, _ = candidates[middle]
            before = (
                order > middle_order
                if descending and order != middle_order
                else order < middle_order
                if not descending and order != middle_order
                else ordinal < middle_ordinal
            )
            if before:
                high = middle
            else:
                low = middle + 1
        candidates.insert(low, (order, ordinal, candidate_output, encoded_bytes))
        bounded_bytes = 2
        for at, (item_order, item_ordinal, _item, item_bytes) in enumerate(candidates):
            addition = item_bytes + (1 if at else 0)
            if bounded_bytes + addition > max_output_bytes:
                candidates[at] = (item_order, item_ordinal, None, item_bytes)
                del candidates[at + 1 :]
                output_limited = True
                break
            bounded_bytes += addition

    bounded = [item for _, _, item, _ in candidates if item is not None]
    input_limited = len(records) > max_records
    return {
        "records": bounded,
        "count": len(bounded),
        "partial": input_limited or output_limited,
        "checkpoint": (
            {"reason": "input_limit", "next_index": max_records}
            if input_limited
            else {"reason": "output_limit", "emitted": len(bounded)}
            if output_limited
            else None
        ),
    }
