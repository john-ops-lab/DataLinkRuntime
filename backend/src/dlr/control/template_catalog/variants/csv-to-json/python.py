"""Bounded CSV to JSON conversion without Managed Input coupling."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

# CSV 转 JSON：可修改的配置集中在这里。
# 运行时提供待处理的数据或文件；处理规则在下面配置。
# 调试时可传入 JSON 对象覆盖同名配置；嵌套对象需要完整填写。
CONFIG = {
    # CSV 字符编码，例如 utf-8 或 utf-8-sig。
    "encoding": "utf-8-sig",
    # CSV 字段分隔符，默认逗号。
    "delimiter": ",",
    # 是否把首行作为字段名。
    "header": True,
    # 是否跳过空行。
    "skip_empty": True,
    # 输入大小上限，单位字节。
    "max_input_bytes": 2097152,
    # 返回结果大小上限，单位字节。
    "max_output_bytes": 4194304,
    # 最多读取的行数。
    "max_rows": 10000,
    # 最多读取的列数。
    "max_columns": 200,
    # 单个字段大小上限，单位字节。
    "max_field_bytes": 65536,
}


_STABLE_ERRORS = frozenset(
    {
        "input_must_be_object",
        "input_too_large",
        "csv_content_required",
        "invalid_encoding_or_content",
        "invalid_delimiter",
        "invalid_headers",
        "invalid_or_duplicate_header",
        "column_limit_exceeded",
        "field_limit_exceeded",
        "row_has_extra_columns",
    }
)


def _positive(value: object, default: int, maximum: int) -> int:
    return (
        min(value, maximum)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else default
    )


def _content(context, input: dict[str, object], max_bytes: int) -> bytes:
    raw = input.get("content")
    if isinstance(raw, str):
        data = raw.encode(str(input.get("encoding", "utf-8-sig")))
    elif context.input_files:
        item = context.input_files[0]
        if item.size_bytes > max_bytes:
            raise ValueError("input_too_large")
        data = Path(item.path).read_bytes()
    else:
        raise ValueError("csv_content_required")
    if len(data) > max_bytes:
        raise ValueError("input_too_large")
    return data


def _validate_headers(headers: list[str], max_columns: int, max_field_bytes: int) -> None:
    invalid_names = len(set(headers)) != len(headers) or any(not item.strip() for item in headers)
    if not headers or invalid_names:
        raise ValueError("invalid_or_duplicate_header")
    if len(headers) > max_columns:
        raise ValueError("column_limit_exceeded")
    if any(len(item.encode("utf-8")) > max_field_bytes for item in headers):
        raise ValueError("field_limit_exceeded")


def _handle(context, input):
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    max_input_bytes = _positive(input.get("max_input_bytes"), 2_097_152, 16_777_216)
    max_output_bytes = _positive(input.get("max_output_bytes"), 4_194_304, 16_777_216)
    max_rows = _positive(input.get("max_rows"), 10_000, 100_000)
    max_columns = _positive(input.get("max_columns"), 200, 2_000)
    max_field_bytes = _positive(input.get("max_field_bytes"), 65_536, 1_048_576)
    encoding = str(input.get("encoding", "utf-8-sig"))
    delimiter = input.get("delimiter", ",")
    if not isinstance(delimiter, str) or len(delimiter) != 1:
        raise ValueError("invalid_delimiter")
    try:
        text = _content(context, input, max_input_bytes).decode(encoding)
    except (LookupError, UnicodeError):
        raise ValueError("invalid_encoding_or_content") from None
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    explicit_headers = input.get("headers")
    use_header = bool(input.get("header", True))
    headers: list[str] | None = None
    if explicit_headers is not None:
        if not isinstance(explicit_headers, list) or not all(
            isinstance(item, str) for item in explicit_headers
        ):
            raise ValueError("invalid_headers")
        headers = list(explicit_headers)
        _validate_headers(headers, max_columns, max_field_bytes)
    rows: list[object] = []
    output_bytes = 2
    partial = False
    checkpoint = None
    for row_number, row in enumerate(reader, start=1):
        if len(row) > max_columns:
            raise ValueError("column_limit_exceeded")
        if any(len(cell.encode("utf-8")) > max_field_bytes for cell in row):
            raise ValueError("field_limit_exceeded")
        if input.get("skip_empty", True) and (not row or all(not cell.strip() for cell in row)):
            continue
        if headers is None and use_header:
            headers = [cell.removeprefix("\ufeff") for cell in row]
            _validate_headers(headers, max_columns, max_field_bytes)
            continue
        item: object
        if headers is None:
            item = row
        else:
            if len(row) > len(headers):
                raise ValueError("row_has_extra_columns")
            item = {
                name: row[index] if index < len(row) else None for index, name in enumerate(headers)
            }
        encoded_bytes = len(
            json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()
        ) + (1 if rows else 0)
        if len(rows) >= max_rows or output_bytes + encoded_bytes > max_output_bytes:
            partial = True
            checkpoint = {"next_physical_row": row_number}
            break
        rows.append(item)
        output_bytes += encoded_bytes
    return {
        "rows": rows,
        "count": len(rows),
        "partial": partial,
        "checkpoint": checkpoint,
        "encoding": encoding,
        "delimiter": delimiter,
    }


def handle(context, input):
    if input is None:
        input = {}
    if not isinstance(input, dict):
        raise ValueError("输入必须是 JSON 对象")
    input = {**CONFIG, **input}
    try:
        return _handle(context, input)
    except ValueError as error:
        code = str(error)
        if code in _STABLE_ERRORS:
            raise ValueError(code) from None
        raise ValueError("csv_operation_failed") from None
    except Exception:
        raise ValueError("csv_operation_failed") from None
