"""Small dependency-free streaming multipart reader for the B1 upload API."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from urllib.parse import unquote_to_bytes


class MultipartParseError(ValueError):
    """Raised for malformed multipart framing or untrusted part metadata."""


class MultipartBodyTooLargeError(MultipartParseError):
    """Raised when the application-level total request budget is exceeded."""


@dataclass(frozen=True)
class MultipartPart:
    """Safe metadata for the currently open multipart part."""

    name: str | None
    filename: str | None
    content_type: str | None


_BOUNDARY_RE = re.compile(r"(?:^|;)\s*boundary\s*=\s*(?:\"([^\"]+)\"|([^;\s]+))", re.IGNORECASE)
_PARAM_RE = re.compile(
    r";\s*([!#$%&'*+.^_`|~0-9A-Za-z-]+)\s*=\s*(?:\"((?:[^\"\\]|\\.)*)\"|([^;]*))"
)


def _header_value(headers: list[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.casefold() == name:
            return value
    return None


def _decode_parameter(value: str, *, encoded: bool = False) -> str:
    try:
        raw = unquote_to_bytes(value) if encoded else value.encode("latin-1")
        return raw.decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        try:
            return raw.decode("latin-1")
        except UnboundLocalError as exc:  # pragma: no cover - defensive only
            raise MultipartParseError("invalid multipart metadata") from exc


def _disposition_parameters(value: str) -> dict[str, str]:
    if not value or value.split(";", 1)[0].strip().casefold() != "form-data":
        raise MultipartParseError("multipart disposition is invalid")
    parameters: dict[str, str] = {}
    for match in _PARAM_RE.finditer(value):
        key = match.group(1).casefold()
        raw = match.group(2) if match.group(2) is not None else (match.group(3) or "").strip()
        if key.endswith("*"):
            raw = raw.split("'", 2)[-1] if "'" in raw else raw
            parameters[key] = _decode_parameter(raw, encoded=True)
        else:
            parameters[key] = _decode_parameter(raw)
    return parameters


class MultipartReader:
    """Incrementally parse one request body without buffering file contents."""

    HEADER_LIMIT = 16 * 1024
    FIELD_LIMIT = 64 * 1024
    EPILOGUE_LIMIT = 8 * 1024
    REQUEST_OVERHEAD_LIMIT = 256 * 1024

    def __init__(
        self,
        stream: AsyncIterable[bytes],
        content_type: str | None,
        *,
        max_total_bytes: int,
    ) -> None:
        if not content_type:
            raise MultipartParseError("multipart content type is missing")
        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or max_total_bytes <= 0
        ):
            raise MultipartParseError("multipart body limit is invalid")
        matched = _BOUNDARY_RE.search(content_type)
        if matched is None:
            raise MultipartParseError("multipart boundary is missing")
        boundary = matched.group(1) or matched.group(2) or ""
        try:
            boundary_bytes = boundary.encode("ascii")
        except UnicodeEncodeError as exc:
            raise MultipartParseError("multipart boundary is invalid") from exc
        if not 1 <= len(boundary_bytes) <= 70 or any(
            byte < 0x20 or byte == 0x7F for byte in boundary_bytes
        ):
            raise MultipartParseError("multipart boundary is invalid")
        self._stream = stream.__aiter__()
        self._buffer = bytearray()
        self._exhausted = False
        self._started = False
        self._finished = False
        self._boundary = boundary_bytes
        self._max_total_bytes = max_total_bytes
        self._received_bytes = 0

    async def _fill(self, minimum: int) -> None:
        while len(self._buffer) < minimum and not self._exhausted:
            try:
                chunk = await self._stream.__anext__()
            except StopAsyncIteration:
                self._exhausted = True
                return
            if not isinstance(chunk, bytes):
                raise MultipartParseError("multipart body is invalid")
            if chunk:
                received_bytes = self._received_bytes + len(chunk)
                if received_bytes > self._max_total_bytes:
                    raise MultipartBodyTooLargeError("multipart body is too large")
                self._received_bytes = received_bytes
                self._buffer.extend(chunk)

    async def _headers(self) -> MultipartPart | None:
        separator = b"\r\n\r\n"
        while True:
            position = self._buffer.find(separator)
            if position >= 0:
                if position > self.HEADER_LIMIT:
                    raise MultipartParseError("multipart headers are too large")
                raw_headers = bytes(self._buffer[:position])
                del self._buffer[: position + len(separator)]
                break
            separator_prefix_bytes = 0
            for prefix_bytes in range(len(separator) - 1, 0, -1):
                if self._buffer.endswith(separator[:prefix_bytes]):
                    separator_prefix_bytes = prefix_bytes
                    break
            if len(self._buffer) - separator_prefix_bytes > self.HEADER_LIMIT:
                raise MultipartParseError("multipart headers are too large")
            if self._exhausted:
                raise MultipartParseError("multipart headers are incomplete")
            await self._fill(len(self._buffer) + 1)
        headers: list[tuple[str, str]] = []
        for raw_line in raw_headers.split(b"\r\n"):
            raw_name, separator, raw_value = raw_line.partition(b":")
            if not separator or not raw_name:
                raise MultipartParseError("multipart headers are invalid")
            try:
                header_name = raw_name.decode("ascii").strip()
                header_value = raw_value.decode("latin-1").strip()
            except UnicodeDecodeError as exc:
                raise MultipartParseError("multipart headers are invalid") from exc
            if not header_name or any(ord(char) < 0x20 for char in header_value):
                raise MultipartParseError("multipart headers are invalid")
            headers.append((header_name, header_value))
        disposition = _header_value(headers, "content-disposition")
        parameters = _disposition_parameters(disposition or "")
        name = parameters.get("name")
        filename = parameters.get("filename*") or parameters.get("filename")
        return MultipartPart(
            name=name,
            filename=filename,
            content_type=_header_value(headers, "content-type"),
        )

    async def next_part(self) -> MultipartPart | None:
        """Finish the previous part and return metadata for the next one."""
        if self._finished:
            return None
        if not self._started:
            self._started = True
            prefix = b"--" + self._boundary
            await self._fill(len(prefix) + 2)
            if not self._buffer.startswith(prefix):
                raise MultipartParseError("multipart preamble is invalid")
            del self._buffer[: len(prefix)]
            await self._fill(2)
            if self._buffer.startswith(b"--"):
                del self._buffer[:2]
                self._finished = True
                return None
            if not self._buffer.startswith(b"\r\n"):
                raise MultipartParseError("multipart boundary is invalid")
            del self._buffer[:2]
        return await self._headers()

    async def iter_part_body(self) -> AsyncIterator[bytes]:
        """Yield bytes up to the next boundary and consume its framing."""
        if not self._started or self._finished:
            raise MultipartParseError("multipart part is not open")
        marker = b"\r\n--" + self._boundary
        while True:
            position = self._buffer.find(marker)
            if position >= 0:
                if position:
                    yield bytes(self._buffer[:position])
                del self._buffer[: position + len(marker)]
                await self._fill(2)
                if self._buffer.startswith(b"--"):
                    del self._buffer[:2]
                    self._finished = True
                    # A final CRLF is conventional; tolerate its absence but
                    # reject non-whitespace trailing bytes in next_part().
                    await self._fill(2)
                    if self._buffer.startswith(b"\r\n"):
                        del self._buffer[:2]
                    return
                if self._buffer.startswith(b"\r\n"):
                    del self._buffer[:2]
                    return
                raise MultipartParseError("multipart boundary suffix is invalid")
            if self._exhausted:
                raise MultipartParseError("multipart body is incomplete")
            # Keep enough bytes to recognize a boundary split across chunks.
            safe = len(self._buffer) - len(marker) + 1
            if safe > 0:
                yield bytes(self._buffer[:safe])
                del self._buffer[:safe]
            await self._fill(len(self._buffer) + 1)

    async def ensure_complete(self) -> None:
        """Consume the epilogue and reject non-whitespace trailing bytes."""
        if not self._finished:
            raise MultipartParseError("multipart body is incomplete")
        epilogue_bytes = 0
        while True:
            if self._buffer:
                epilogue_bytes += len(self._buffer)
                invalid = bool(self._buffer.strip())
                self._buffer.clear()
                if epilogue_bytes > self.EPILOGUE_LIMIT:
                    raise MultipartParseError("multipart epilogue is too large")
                if invalid:
                    raise MultipartParseError("multipart epilogue is invalid")
            if self._exhausted:
                return
            await self._fill(1)


__all__ = [
    "MultipartBodyTooLargeError",
    "MultipartParseError",
    "MultipartPart",
    "MultipartReader",
]
