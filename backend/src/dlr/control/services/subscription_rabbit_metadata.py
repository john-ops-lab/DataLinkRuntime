"""Bounded AMQP property preview; reads wire structure without decoding values."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from dlr.control.services.subscription_source import SourceFailure


class PropertyPreview:
    def __init__(self, data: bytes, start: int, end: int):
        self.data, self.offset, self.end = data, start, end
        self.nodes = 0
        self.values = 1  # headers root table; same per-property typed budget

    def advance(self, count: int) -> None:
        self.offset += count
        if self.offset > self.end:
            raise SourceFailure("source_properties_invalid")

    def number(self, size: int) -> int:
        start = self.offset
        self.advance(size)
        return int.from_bytes(memoryview(self.data)[start : self.offset], "big")

    def short_string(self) -> None:
        self.advance(self.number(1))

    def container(self, depth: int, *, table: bool) -> None:
        end = self.offset + 4 + self.number(4)
        if end > self.end:
            raise SourceFailure("source_properties_invalid")
        while self.offset < end:
            if table:
                self.short_string()
                self.nodes += 1  # decoded dictionary key
            self.value(depth + 1)
        if self.offset != end:
            raise SourceFailure("source_properties_invalid")

    def value(self, depth: int) -> None:
        self.nodes += 1
        self.values += 1
        if depth > 12 or self.values > 4096:
            raise SourceFailure("source_properties_invalid")
        kind = self.number(1)
        widths = {
            116: 1,
            98: 1,
            66: 1,
            85: 2,
            117: 2,
            115: 2,
            73: 4,
            105: 4,
            76: 8,
            108: 8,
            102: 4,
            100: 8,
            68: 5,
            84: 8,
            86: 0,
        }
        if kind in widths:
            self.advance(widths[kind])
        elif kind in {83, 120}:  # long string / bytes
            self.advance(self.number(4))
        elif kind in {65, 70}:  # array / table
            self.container(depth, table=kind == 70)
        else:
            raise SourceFailure("source_properties_invalid")


def preview_header(data: bytes, payload: int) -> int:
    """SDK object graph plus decoder scratch, before Pika slices the payload.

    Container nodes are separate from long text. Four bytes per wire byte cover
    Python's widest Unicode representation; 256 per node covers decoded scalar,
    dictionary/list slots and recursive decoder scratch. Raw frame copies are
    already covered by the connection workspace. Typed wrappers are booked
    separately, after the actual SDK graph can be measured, before conversion.
    """
    scan = PropertyPreview(data, 19, 7 + payload)
    flags, shift = 0, 0
    while True:
        word = scan.number(2)
        flags |= word << shift
        if not word & 1:
            break
        shift += 16
        if shift > 16:
            raise SourceFailure("source_properties_invalid")
    for bit in range(15, 1, -1):
        if not flags & (1 << bit):
            continue
        if bit == 13:
            scan.nodes += 1
            scan.container(0, table=True)
        elif bit in {12, 11}:
            scan.advance(1)
        elif bit == 6:
            scan.advance(8)
        else:
            scan.short_string()
        scan.nodes += 1
    if scan.offset != scan.end:
        raise SourceFailure("source_properties_invalid")
    return 8192 + payload * 4 + scan.nodes * 256


def heap_size(value: Any, depth: int = 0) -> int:
    """Conservative graph size: shared immutable objects are counted twice."""
    if depth > 40:
        raise SourceFailure("source_properties_invalid")
    result = sys.getsizeof(value)
    if isinstance(value, dict):
        for key, child in value.items():
            result += heap_size(key, depth + 1) + heap_size(child, depth + 1)
    elif isinstance(value, list):
        result += sum(heap_size(child, depth + 1) for child in value)
    return result


def typed_scratch(value: Any, depth: int = 0, budget: list[int] | None = None) -> int:
    """Book additional objects before typed_property constructs them."""
    budget = [4096] if budget is None else budget
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise SourceFailure("source_properties_invalid")
    if value is None or isinstance(value, (bool, str)):
        return 0
    if isinstance(value, bytes):
        return 512 + 2 * (4 * ((len(value) + 2) // 3))
    if isinstance(value, (Decimal, datetime, int, float)):
        text = value.isoformat() if isinstance(value, datetime) else str(value)
        return 256 + sys.getsizeof(text)
    if isinstance(value, list):
        return (
            64 + 16 * len(value) + sum(typed_scratch(child, depth + 1, budget) for child in value)
        )
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            512
            + 64 * len(value)
            + sum(typed_scratch(child, depth + 1, budget) for child in value.values())
        )
    raise SourceFailure("source_properties_invalid")


@dataclass
class JsonShape:
    utf8: int = 0
    chars: int = 0
    ascii: int = 0
    width: int = 1
    string_utf8: int = 0
    string_wide: int = 0
    invalid: bool = False


def json_shape(value: Any) -> JsonShape:
    """Size compact JSON without producing a second text/byte representation."""
    result = JsonShape()

    def add(size: int) -> None:
        result.utf8 += size
        result.chars += size
        result.ascii += size

    def string(text: str) -> None:
        utf8, chars, ascii_size, width = 2, 2, 2, 1
        for char in text:
            code = ord(char)
            if code == 0 or 0xD800 <= code <= 0xDFFF:
                result.invalid = True
            if char in {'"', "\\"} or code in {8, 9, 10, 12, 13}:
                utf8 += 2
                chars += 2
                ascii_size += 2
            elif code < 32:
                utf8 += 6
                chars += 6
                ascii_size += 6
            else:
                utf8 += 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
                chars += 1
                ascii_size += 1 if code < 128 else 6 if code < 65536 else 12
                width = max(width, 1 if code < 256 else 2 if code < 65536 else 4)
        result.utf8 += utf8
        result.chars += chars
        result.ascii += ascii_size
        result.width = max(result.width, width)
        result.string_utf8 = max(result.string_utf8, utf8)
        result.string_wide = max(result.string_wide, chars * width)

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 40:
            raise SourceFailure("source_properties_invalid")
        if isinstance(item, str):
            string(item)
        elif item is None:
            add(4)
        elif isinstance(item, bool):
            add(4 if item else 5)
        elif isinstance(item, (int, float)):
            add(len(str(item)))
        elif isinstance(item, (dict, list)):
            add(2 + max(0, len(item) - 1))
            if isinstance(item, dict):
                for key, child in item.items():
                    string(key)
                    add(1)
                    walk(child, depth + 1)
            else:
                for child in item:
                    walk(child, depth + 1)
        else:
            raise SourceFailure("source_properties_invalid")

    walk(value)
    return result


def envelope_workspace(shape: JsonShape, metadata: JsonShape, raw_size: int) -> int:
    """Base64, validation/canonicalization and DB JSON peaks are successive."""
    encoded = 4 * ((raw_size + 2) // 3)
    utf8 = shape.utf8 + encoded
    chars = shape.chars + encoded
    ascii_size = shape.ascii + encoded
    string_utf8 = max(shape.string_utf8, encoded + 2)
    string_wide = max(shape.string_wide, encoded + 2)
    return 8192 + max(
        2 * encoded,
        encoded + shape.width * chars + utf8 + 256,
        encoded + 2 * ascii_size,
        encoded + 2 * utf8 + string_utf8 + string_wide,
        encoded + raw_size + 2 * metadata.utf8,
    )
