"""Bounded read-only MySQL snapshot."""

from __future__ import annotations

import base64
import json
import math
import re
from collections.abc import Mapping
from contextlib import suppress
from datetime import date, datetime, time
from decimal import Decimal
from urllib.parse import unquote, urlsplit
from uuid import UUID

_SELECT = re.compile(r"\A\s*select\b", re.IGNORECASE)


def _positive(value: object, default: int, maximum: int) -> int:
    return (
        min(value, maximum)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else default
    )


def _query(input: dict[str, object]) -> str:
    sql = input.get("sql")
    if not isinstance(sql, str) or not _SELECT.match(sql) or ";" in sql:
        raise ValueError("single_select_required")
    forbidden = re.compile(
        r"\b(insert|update|delete|merge|call|execute|create|alter|drop|truncate|copy)\b",
        re.IGNORECASE,
    )
    if forbidden.search(sql):
        raise ValueError("read_only_query_required")
    return sql


def _connection_options(dsn: str) -> dict[str, object]:
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"mysql", "mysql+pymysql"}:
        raise ValueError("invalid_mysql_dsn")
    if not parsed.hostname or parsed.username is None:
        raise ValueError("invalid_mysql_dsn")
    database = unquote(parsed.path.lstrip("/"))
    if not database or "/" in database:
        raise ValueError("invalid_mysql_dsn")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username),
        "password": unquote(parsed.password or ""),
        "database": database,
    }


class _UnsupportedCell(TypeError):
    pass


def _normalize_cell(value: object, depth: int = 0) -> object:
    if depth > 32:
        raise _UnsupportedCell
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _UnsupportedCell
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return {"$binary_base64": encoded}
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _UnsupportedCell
            normalized[key] = _normalize_cell(item, depth + 1)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_cell(item, depth + 1) for item in value]
    raise _UnsupportedCell


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _column_names(cursor: object, raw_row: object) -> list[str]:
    description = getattr(cursor, "description", None)
    if description:
        names = [str(item[0]) for item in description]
    elif isinstance(raw_row, Mapping):
        names = [str(name) for name in raw_row]
    else:
        raise ValueError("missing_column_metadata")
    if len(names) != len(set(names)):
        raise ValueError("duplicate_column_label")
    return names


def _row_values(raw_row: object, names: list[str]) -> list[object]:
    if isinstance(raw_row, Mapping):
        return [raw_row[name] for name in names]
    values = list(raw_row)  # type: ignore[arg-type]
    if len(values) != len(names):
        raise ValueError("column_count_mismatch")
    return values


def handle(context, input):
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    sql = _query(input)
    params = input.get("params", [])
    if not isinstance(params, list) or len(params) > 64:
        raise ValueError("params_must_be_array")
    max_rows = _positive(input.get("max_rows"), 5_000, 100_000)
    batch_size = _positive(input.get("batch_size"), 500, 5_000)
    max_output_bytes = _positive(input.get("max_output_bytes"), 4_194_304, 16_777_216)
    max_cell_bytes = _positive(input.get("max_cell_bytes"), 1_048_576, 8_388_608)
    timeout = _positive(input.get("timeout_seconds"), 30, 300)
    dsn = context.secrets.get("MYSQL_DSN")
    if not dsn:
        raise ValueError("missing_credential")
    rows: list[dict[str, object]] = []
    connection = None
    failure_code = "database_connection_failed"
    output_bytes = 2
    partial = False
    try:
        import pymysql

        try:
            from pymysql.cursors import SSCursor
        except ImportError:  # Compatibility with minimal driver test doubles.
            from pymysql.cursors import SSDictCursor as SSCursor

        connection = pymysql.connect(
            **_connection_options(dsn),
            read_timeout=timeout,
            write_timeout=timeout,
            cursorclass=SSCursor,
            autocommit=False,
        )
        failure_code = "database_query_failed"
        with connection.cursor() as control:
            control.execute("SET SESSION TRANSACTION READ ONLY")
            control.execute("SET time_zone = '+00:00'")
            control.execute("SET SESSION MAX_EXECUTION_TIME=%s", (timeout * 1000,))
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            names: list[str] | None = (
                _column_names(cursor, {}) if getattr(cursor, "description", None) else None
            )
            while not partial:
                batch = cursor.fetchmany(min(batch_size, max_rows + 1 - len(rows)))
                if not batch:
                    break
                for raw_row in batch:
                    if len(rows) >= max_rows:
                        partial = True
                        break
                    if names is None:
                        names = _column_names(cursor, raw_row)
                    try:
                        values = [_normalize_cell(value) for value in _row_values(raw_row, names)]
                    except _UnsupportedCell:
                        partial = True
                        break
                    if any(len(_json_bytes(value)) > max_cell_bytes for value in values):
                        partial = True
                        break
                    row = dict(zip(names, values, strict=True))
                    encoded_bytes = len(_json_bytes(row)) + (1 if rows else 0)
                    if output_bytes + encoded_bytes > max_output_bytes:
                        partial = True
                        break
                    rows.append(row)
                    output_bytes += encoded_bytes

        connection.rollback()
        return {
            "rows": rows,
            "count": len(rows),
            "partial": partial,
            "checkpoint": {"row_offset": len(rows)} if partial else None,
        }
    except Exception:
        if connection is not None:
            with suppress(Exception):
                connection.rollback()
        return {
            "rows": rows,
            "count": len(rows),
            "partial": True,
            "error": failure_code,
        }
    finally:
        if connection is not None:
            with suppress(Exception):
                connection.close()
