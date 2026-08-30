"""M5.7 Wave B2: bounded, request-only attachment handling for the AI Editor.

Contract summary (the Wave B3 UI and every future caller rely on it):

- Attachments travel inside the existing JSON ``/ai/assist`` request as
  ``attachments: [{filename, content_type, data_base64}]``. Requests without
  the field keep the pre-attachment behavior byte-for-byte.
- Every limit below is enforced server-side with a stable error ``code``:
  ``ai_attachment_invalid`` / ``ai_attachment_filename_invalid`` /
  ``ai_attachment_type_unsupported`` / ``ai_attachment_too_large`` /
  ``ai_attachment_total_too_large`` / ``ai_attachment_count_exceeded`` /
  ``ai_attachment_image_unsupported`` / ``ai_attachment_parse_failed`` /
  ``ai_attachment_no_text`` / ``ai_attachment_unsafe_archive`` /
  ``ai_attachment_parse_timeout``. Error ``detail`` never echoes file
  content, filenames or base64 data.
- Images are only accepted when the Provider capability table says the
  Provider natively supports them; DLR never OCRs an image to fake vision.
- PDF / DOCX / XLS / XLSX / text / code are parsed server-side into bounded UTF-8 text
  that joins the request context. DOCX uses only the stdlib (``zipfile`` +
  ``xml.etree``); XLS uses the pinned, in-memory ``xlrd`` BIFF parser; PDF uses
  the pinned, pure-Python ``pypdf`` (BSD-3-Clause, no transitive deps,
  headless-container friendly).
- Everything stays in memory: no temp files, no database rows, no Thread,
  no logs. Success, validation failure, parse failure and timeout all leave
  nothing behind.
"""

import base64
import binascii
import io
import threading
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

import xlrd
from pypdf import PdfReader
from pypdf.errors import PdfReadError

# ---------------------------------------------------------------------------
# Bounded limits (the single source of truth for the capability endpoint).
# ---------------------------------------------------------------------------

MAX_ATTACHMENTS = 8
MAX_FILE_BYTES = 6 * 1024 * 1024  # per-file decoded size
MAX_TOTAL_BYTES = MAX_ATTACHMENTS * MAX_FILE_BYTES  # sum of decoded sizes
MAX_PARSED_CHARS_PER_FILE = 64 * 1024  # extracted text budget per file
MAX_PARSED_TOTAL_CHARS = 256 * 1024  # total extracted text budget
PARSE_TIMEOUT_SECONDS = 30.0

# DOCX (zip) decompression-bomb guards: no single member may inflate past the
# member cap, the archive's total uncompressed size stays below the total cap
# and the inflation ratio stays bounded.
MAX_DOCX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DOCX_INFLATION_RATIO = 50
ZIP_LOCAL_FILE_HEADER_SIGNATURE = b"PK\x03\x04"
ZIP_LOCAL_FILE_HEADER_SIZE = 30

# xlrd parses the complete BIFF workbook in memory. These additional iteration
# bounds keep a workbook with a very large declared sheet dimension from
# spending the entire parser deadline walking empty rows/cells after the file
# has already passed the 6 MiB decoded-size limit.
MAX_XLS_ROWS = 100_000
MAX_XLS_COLUMNS = 16_384

TRUNCATION_MARKER = "\n...[attachment text truncated by DLR context bound]..."


class AttachmentError(Exception):
    """Stable attachment failure carrying only a public error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


ATTACHMENT_ERROR_STATUS: dict[str, int] = {
    "ai_attachment_invalid": 422,
    "ai_attachment_filename_invalid": 422,
    "ai_attachment_type_unsupported": 422,
    "ai_attachment_too_large": 422,
    "ai_attachment_total_too_large": 422,
    "ai_attachment_count_exceeded": 422,
    "ai_attachment_image_unsupported": 422,
    "ai_attachment_parse_failed": 422,
    "ai_attachment_no_text": 422,
    "ai_attachment_unsafe_archive": 422,
    "ai_attachment_parse_timeout": 504,
}

IMAGE_CATEGORY = "image"
PDF_CATEGORY = "pdf"
DOCX_CATEGORY = "docx"
XLS_CATEGORY = "xls"
XLSX_CATEGORY = "xlsx"
TEXT_CATEGORY = "text"
CODE_CATEGORY = "code"
XLS_MIME = "application/vnd.ms-excel"
XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_TEXT_EXTENSIONS = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "csv",
        "json",
        "yaml",
        "yml",
        "xml",
        "toml",
        "ini",
        "conf",
        "cfg",
        "properties",
        "env",
    }
)
_CODE_EXTENSIONS = frozenset(
    {
        "py",
        "js",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "jsx",
        "go",
        "rs",
        "java",
        "kt",
        "sh",
        "bash",
        "sql",
        "html",
        "css",
        "yaml",
        "yml",
        "json",
        "xml",
        "toml",
    }
)

# content_type -> allowed extensions. Both must agree; unknown MIME values and
# MIME/extension mismatches are rejected as unsupported/fake types.
MIME_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/png": frozenset({"png"}),
    "image/jpeg": frozenset({"jpg", "jpeg"}),
    "image/webp": frozenset({"webp"}),
    "application/pdf": frozenset({"pdf"}),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": frozenset({"docx"}),
    XLS_MIME: frozenset({"xls"}),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": frozenset({"xlsx"}),
    "text/plain": _TEXT_EXTENSIONS | _CODE_EXTENSIONS,
    "text/markdown": frozenset({"md", "markdown"}),
    "text/csv": frozenset({"csv"}),
    "application/json": frozenset({"json"}),
    "text/x-yaml": frozenset({"yaml", "yml"}),
    "application/x-yaml": frozenset({"yaml", "yml"}),
    "text/xml": frozenset({"xml"}),
    "application/xml": frozenset({"xml"}),
    "text/javascript": frozenset({"js", "mjs", "cjs"}),
    "application/javascript": frozenset({"js", "mjs", "cjs"}),
    # Browsers commonly label .py/.yaml/.json uploads as octet-stream; accept
    # it only when the extension is a whitelisted text/code extension AND the
    # content decodes as strict UTF-8 text with no NUL bytes.
    "application/octet-stream": _TEXT_EXTENSIONS | _CODE_EXTENSIONS,
}

SUPPORTED_CONTENT_TYPES = tuple(sorted(MIME_EXTENSIONS))

# Maps every accepted MIME to its category (used by the prompt context).
_MIME_CATEGORY: dict[str, str] = {
    "image/png": IMAGE_CATEGORY,
    "image/jpeg": IMAGE_CATEGORY,
    "image/webp": IMAGE_CATEGORY,
    "application/pdf": PDF_CATEGORY,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCX_CATEGORY,
    XLS_MIME: XLS_CATEGORY,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": XLSX_CATEGORY,
    "text/plain": TEXT_CATEGORY,
    "text/markdown": TEXT_CATEGORY,
    "text/csv": TEXT_CATEGORY,
    "application/json": CODE_CATEGORY,
    "text/x-yaml": CODE_CATEGORY,
    "application/x-yaml": CODE_CATEGORY,
    "text/xml": CODE_CATEGORY,
    "application/xml": CODE_CATEGORY,
    "text/javascript": CODE_CATEGORY,
    "application/javascript": CODE_CATEGORY,
    "application/octet-stream": TEXT_CATEGORY,
}

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def limits() -> dict[str, int | float]:
    """Expose the bounded limits for the capability endpoint and the UI."""
    return {
        "max_attachments": MAX_ATTACHMENTS,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_parsed_chars_per_file": MAX_PARSED_CHARS_PER_FILE,
        "max_parsed_total_chars": MAX_PARSED_TOTAL_CHARS,
        "parse_timeout_seconds": PARSE_TIMEOUT_SECONDS,
    }


def supported_content_types() -> list[str]:
    return list(SUPPORTED_CONTENT_TYPES)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def sanitize_filename(filename: object) -> str:
    """Return a display-only sanitized filename or raise a stable error.

    The filename is never used as a filesystem path anywhere in DLR; this
    check exists to reject path / traversal / control-character injection
    before the name is forwarded into the Provider context.
    """
    if not isinstance(filename, str):
        raise AttachmentError("ai_attachment_invalid")
    stripped = filename.strip()
    if not stripped:
        raise AttachmentError("ai_attachment_filename_invalid")
    if len(stripped) > 255:
        raise AttachmentError("ai_attachment_filename_invalid")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in stripped):
        raise AttachmentError("ai_attachment_filename_invalid")
    if (
        "/" in stripped
        or "\\" in stripped
        or stripped == "."
        or stripped == ".."
        or stripped.startswith(".")
    ):
        raise AttachmentError("ai_attachment_filename_invalid")
    return stripped


def _extension_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].strip().lower()


def classify(filename: str, content_type: object) -> tuple[str, str]:
    """Return (category, normalized_content_type) or raise a stable error.

    Both the declared MIME and the filename extension must match the
    whitelist; unknown MIME values and MIME/extension mismatches (fake or
    mistyped types) are rejected with ``ai_attachment_type_unsupported``.
    """
    if not isinstance(content_type, str):
        raise AttachmentError("ai_attachment_type_unsupported")
    normalized = content_type.split(";", 1)[0].strip().lower()
    allowed_extensions = MIME_EXTENSIONS.get(normalized)
    if allowed_extensions is None:
        raise AttachmentError("ai_attachment_type_unsupported")
    extension = _extension_of(filename)
    if not extension or extension not in allowed_extensions:
        raise AttachmentError("ai_attachment_type_unsupported")
    return _MIME_CATEGORY[normalized], normalized


def decode_data(data_base64: object) -> bytes:
    """Strictly decode one base64 body, enforcing the per-file size bound."""
    if not isinstance(data_base64, str):
        raise AttachmentError("ai_attachment_invalid")
    # Cheap pre-guard: base64 is ~4/3 of the decoded size plus padding.
    if len(data_base64) > (MAX_FILE_BYTES * 4 // 3) + 8:
        raise AttachmentError("ai_attachment_too_large")
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (ValueError, binascii.Error):
        raise AttachmentError("ai_attachment_invalid") from None
    if not data:
        raise AttachmentError("ai_attachment_invalid")
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError("ai_attachment_too_large")
    return data


def _sniff(content_type: str, data: bytes) -> None:
    """Reject declared types that do not match the actual file signature.

    The declared MIME stays authoritative; this only filters fake/mistyped
    files. Text categories are verified by strict UTF-8 decoding in the
    parser instead of a signature.
    """
    if content_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AttachmentError("ai_attachment_type_unsupported")
    if content_type == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise AttachmentError("ai_attachment_type_unsupported")
    if content_type == "image/webp" and not (
        len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ):
        raise AttachmentError("ai_attachment_type_unsupported")
    if content_type == "application/pdf" and not data.startswith(b"%PDF-"):
        raise AttachmentError("ai_attachment_type_unsupported")
    if (
        content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        and not data.startswith(b"PK\x03\x04")
    ):
        raise AttachmentError("ai_attachment_type_unsupported")
    if (
        content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        and not data.startswith(b"PK\x03\x04")
    ):
        raise AttachmentError("ai_attachment_type_unsupported")
    if content_type == XLS_MIME and not data.startswith(XLS_MAGIC):
        raise AttachmentError("ai_attachment_type_unsupported")


# ---------------------------------------------------------------------------
# Parsing (bounded text extraction; never writes files)
# ---------------------------------------------------------------------------


def _truncate(text: str, limit_chars: int) -> tuple[str, bool]:
    """Truncate extracted text to a char budget with an explicit marker."""
    if limit_chars <= 0:
        return "", True
    if len(text) <= limit_chars:
        return text, False
    marker_len = len(TRUNCATION_MARKER)
    if limit_chars <= marker_len:
        return TRUNCATION_MARKER[:limit_chars], True
    return text[: limit_chars - marker_len] + TRUNCATION_MARKER, True


def _parse_text(data: bytes, limit_chars: int) -> tuple[str, bool]:
    """Strict UTF-8 text extraction for text/code categories."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AttachmentError("ai_attachment_parse_failed") from None
    if "\x00" in text:
        # NUL bytes mean the payload is binary masquerading as text.
        raise AttachmentError("ai_attachment_parse_failed")
    if not text.strip():
        # Empty or whitespace-only text files carry no extractable content:
        # the no-text contract is consistent with the PDF and DOCX paths.
        raise AttachmentError("ai_attachment_no_text")
    return _truncate(text, limit_chars)


def _parse_pdf(data: bytes, limit_chars: int) -> tuple[str, bool]:
    """Extract bounded page text with pypdf; scanned pages yield no text."""
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError, TypeError, KeyError, RecursionError):
        raise AttachmentError("ai_attachment_parse_failed") from None
    chunks: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except (PdfReadError, ValueError, TypeError, KeyError, RecursionError):
            # One corrupt page must not sink the whole document; unreadable
            # pages simply contribute nothing.
            continue
        if not page_text:
            continue
        chunks.append(page_text)
        total += len(page_text)
        if total > limit_chars:
            break
    if not chunks:
        raise AttachmentError("ai_attachment_no_text")
    joined = "".join(chunks)
    if not joined.strip():
        # Whitespace-only pages are text-less documents too: the no-text
        # contract is consistent across every category.
        raise AttachmentError("ai_attachment_no_text")
    return _truncate(joined, limit_chars)


def _actual_archive_member_size(data: bytes, info: zipfile.ZipInfo) -> int:
    """Verify one ZIP member against its actual bounded decompression stream.

    ``ZipInfo.file_size`` and CRC come from the attacker-controlled central
    directory.  Python's ``ZipExtFile`` intentionally stops at that declared
    size, so a forged short size/CRC can hide a much larger deflate stream.
    Read the raw member stream with a bounded decompressor before any XML
    parser is allowed to trust the archive metadata.
    """
    header_start = info.header_offset
    header_end = header_start + ZIP_LOCAL_FILE_HEADER_SIZE
    if header_start < 0 or header_end > len(data):
        raise AttachmentError("ai_attachment_unsafe_archive")
    header = data[header_start:header_end]
    if header[:4] != ZIP_LOCAL_FILE_HEADER_SIGNATURE:
        raise AttachmentError("ai_attachment_unsafe_archive")
    compression_method = int.from_bytes(header[8:10], "little")
    filename_length = int.from_bytes(header[26:28], "little")
    extra_length = int.from_bytes(header[28:30], "little")
    payload_start = header_end + filename_length + extra_length
    payload_end = payload_start + info.compress_size
    if (
        info.flag_bits & 0x1
        or compression_method != info.compress_type
        or payload_start < header_end
        or payload_end < payload_start
        or payload_end > len(data)
    ):
        raise AttachmentError("ai_attachment_unsafe_archive")

    compressed = data[payload_start:payload_end]
    checksum = 0
    if info.compress_type == zipfile.ZIP_STORED:
        actual_size = len(compressed)
        checksum = zlib.crc32(compressed)
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        actual_size = 0
        for offset in range(0, len(compressed), 64 * 1024):
            pending = compressed[offset : offset + 64 * 1024]
            while pending:
                remaining = MAX_DOCX_MEMBER_BYTES + 1 - actual_size
                try:
                    output = decompressor.decompress(pending, remaining)
                except zlib.error:
                    raise AttachmentError("ai_attachment_parse_failed") from None
                actual_size += len(output)
                checksum = zlib.crc32(output, checksum)
                if actual_size > MAX_DOCX_MEMBER_BYTES:
                    raise AttachmentError("ai_attachment_unsafe_archive")
                next_pending = decompressor.unconsumed_tail
                if next_pending and not output and len(next_pending) == len(pending):
                    raise AttachmentError("ai_attachment_parse_failed")
                pending = next_pending
        if not decompressor.eof or decompressor.unused_data:
            raise AttachmentError("ai_attachment_parse_failed")
    else:
        # OOXML only needs stored/deflated members. Other methods cannot be
        # independently size-verified here and are rejected before parsing.
        raise AttachmentError("ai_attachment_unsafe_archive")

    if actual_size != info.file_size or checksum & 0xFFFFFFFF != info.CRC:
        raise AttachmentError("ai_attachment_unsafe_archive")
    return actual_size


def _check_archive_safety(archive: zipfile.ZipFile, data: bytes) -> None:
    infos = archive.infolist()
    uncompressed_total = 0
    compressed_total = 0
    for info in infos:
        name = info.filename
        if not name or name.startswith("/") or "\\" in name or ".." in name:
            raise AttachmentError("ai_attachment_unsafe_archive")
        actual_size = _actual_archive_member_size(data, info)
        uncompressed_total += actual_size
        compressed_total += info.compress_size
        if uncompressed_total > MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES:
            raise AttachmentError("ai_attachment_unsafe_archive")
    if compressed_total > 0 and uncompressed_total > compressed_total * MAX_DOCX_INFLATION_RATIO:
        raise AttachmentError("ai_attachment_unsafe_archive")


def _parse_docx(data: bytes, limit_chars: int) -> tuple[str, bool]:
    """Extract the DOCX document body text with the stdlib only."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise AttachmentError("ai_attachment_parse_failed") from None
    _check_archive_safety(archive, data)
    try:
        with archive.open("word/document.xml") as handle:
            xml_data = handle.read(MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES + 1)
    except KeyError:
        raise AttachmentError("ai_attachment_parse_failed") from None
    except (RuntimeError, zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
        # RuntimeError covers encrypted members; NotImplementedError covers
        # unsupported compression methods.
        raise AttachmentError("ai_attachment_parse_failed") from None
    if len(xml_data) > MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES:
        raise AttachmentError("ai_attachment_unsafe_archive")

    chunks: list[str] = []
    total = 0
    try:
        # Streaming parse keeps memory bounded; only w:t text and paragraph
        # breaks are kept, and extraction stops at the char budget.
        for _, element in ET.iterparse(io.BytesIO(xml_data), events=("end",)):
            if element.tag == f"{_WORD_NS}t":
                if element.text:
                    chunks.append(element.text)
                    total += len(element.text)
                    if total > limit_chars:
                        break
            elif element.tag == f"{_WORD_NS}p":
                chunks.append("\n")
            element.clear()
    except ET.ParseError:
        raise AttachmentError("ai_attachment_parse_failed") from None
    joined = "".join(chunks)
    if not joined.strip():
        # A document whose paragraphs carry no w:t text (empty document, or
        # content only in constructs this extractor skips) is text-less: the
        # no-text contract is consistent with the PDF and text paths.
        raise AttachmentError("ai_attachment_no_text")
    return _truncate(joined, limit_chars)


def _xls_cell_text(cell: Any) -> str:
    """Convert one xlrd cell to a safe display value without evaluating it."""
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return ""
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.value is None:
        return ""
    return str(cell.value)


def _parse_xls(data: bytes, limit_chars: int) -> tuple[str, bool]:
    """Extract bounded cell values from a legacy BIFF workbook in memory."""
    workbook: Any | None = None
    try:
        workbook = xlrd.open_workbook(
            file_contents=data,
            logfile=io.StringIO(),
            verbosity=0,
            use_mmap=False,
            on_demand=True,
            ragged_rows=True,
        )
        rows: list[str] = []
        total_chars = 0
        row_count = 0
        dimension_truncated = False
        for sheet_index in range(workbook.nsheets):
            sheet = workbook.sheet_by_index(sheet_index)
            for row in sheet.get_rows():
                row_count += 1
                if row_count > MAX_XLS_ROWS:
                    dimension_truncated = True
                    break
                values: list[str] = []
                for column_index, cell in enumerate(row):
                    if column_index >= MAX_XLS_COLUMNS:
                        dimension_truncated = True
                        break
                    values.append(_xls_cell_text(cell))
                row_text = "\t".join(values)
                if row_text.strip():
                    rows.append(row_text)
                    total_chars += len(row_text)
                    if total_chars > limit_chars:
                        break
            if total_chars > limit_chars:
                break
        joined = "\n".join(rows)
        if not joined.strip():
            raise AttachmentError("ai_attachment_no_text")
        if dimension_truncated:
            joined = f"{joined}\n{TRUNCATION_MARKER}"
            bounded, _ = _truncate(joined, limit_chars)
            return bounded, True
        return _truncate(joined, limit_chars)
    except AttachmentError:
        raise
    except Exception:  # noqa: BLE001 - xlrd exceptions are intentionally sanitized
        raise AttachmentError("ai_attachment_parse_failed") from None
    finally:
        if workbook is not None:
            with suppress(Exception):  # noqa: BLE001 - cleanup must not leak parser details
                workbook.release_resources()


def _xlsx_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xlsx_text(element: ET.Element) -> str:
    return "".join(
        child.text or ""
        for child in element.iter()
        if isinstance(child.tag, str) and _xlsx_local_name(child.tag) == "t"
    )


def _parse_xlsx(data: bytes, limit_chars: int) -> tuple[str, bool]:
    """Extract worksheet cell values from an XLSX archive in memory.

    XLSX is a ZIP of XML parts. The same archive member and inflation bounds
    used by DOCX are applied before reading anything, and only shared strings
    and worksheet cell values are retained. Legacy binary .xls is dispatched
    to the separate xlrd parser and never reaches this parser.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise AttachmentError("ai_attachment_parse_failed") from None

    try:
        _check_archive_safety(archive, data)
        names = set(archive.namelist())
        if "xl/workbook.xml" not in names:
            raise AttachmentError("ai_attachment_parse_failed")
        worksheet_names = sorted(
            name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")
        )
        if not worksheet_names:
            raise AttachmentError("ai_attachment_parse_failed")

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                with archive.open("xl/sharedStrings.xml") as handle:
                    current_parts: list[str] | None = None
                    shared_budget = limit_chars
                    for event, element in ET.iterparse(handle, events=("start", "end")):
                        local_name = (
                            _xlsx_local_name(element.tag) if isinstance(element.tag, str) else ""
                        )
                        if event == "start" and local_name == "si":
                            current_parts = []
                        elif event == "end":
                            if local_name == "t" and current_parts is not None:
                                current_parts.append(element.text or "")
                            elif local_name == "si":
                                value = "".join(current_parts or [])
                                kept = value[: max(shared_budget, 0)]
                                shared_strings.append(kept)
                                shared_budget -= len(kept)
                                current_parts = None
                            element.clear()
            except (RuntimeError, zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
                raise AttachmentError("ai_attachment_parse_failed") from None

        rows: list[str] = []
        total_chars = 0
        for worksheet_name in worksheet_names:
            try:
                with archive.open(worksheet_name) as handle:
                    row_values: list[str] = []
                    for _event, element in ET.iterparse(handle, events=("end",)):
                        local_name = (
                            _xlsx_local_name(element.tag) if isinstance(element.tag, str) else ""
                        )
                        if local_name == "c":
                            cell_type = element.attrib.get("t", "")
                            value_node = next(
                                (
                                    child
                                    for child in element
                                    if isinstance(child.tag, str)
                                    and _xlsx_local_name(child.tag) == "v"
                                ),
                                None,
                            )
                            raw_value = "" if value_node is None else value_node.text or ""
                            if cell_type == "s":
                                try:
                                    shared_index = int(raw_value)
                                except (TypeError, ValueError):
                                    cell_value = ""
                                else:
                                    cell_value = (
                                        shared_strings[shared_index]
                                        if 0 <= shared_index < len(shared_strings)
                                        else ""
                                    )
                            elif cell_type == "inlineStr":
                                cell_value = _xlsx_text(element)
                            elif cell_type == "b":
                                cell_value = (
                                    "TRUE"
                                    if raw_value == "1"
                                    else "FALSE"
                                    if raw_value == "0"
                                    else raw_value
                                )
                            else:
                                # Numeric values, formulas and error values all
                                # expose their safe displayed value through v.
                                cell_value = raw_value
                            row_values.append(cell_value)
                            element.clear()
                        elif local_name == "row":
                            row_text = "\t".join(row_values)
                            if row_text.strip():
                                rows.append(row_text)
                                total_chars += len(row_text)
                                if total_chars > limit_chars:
                                    break
                            row_values = []
                            element.clear()
            except (RuntimeError, zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
                raise AttachmentError("ai_attachment_parse_failed") from None
            if total_chars > limit_chars:
                break

        joined = "\n".join(rows)
        if not joined.strip():
            raise AttachmentError("ai_attachment_no_text")
        return _truncate(joined, limit_chars)
    except ET.ParseError:
        raise AttachmentError("ai_attachment_parse_failed") from None
    finally:
        archive.close()


# ---------------------------------------------------------------------------
# Timeout guard and public entry point
# ---------------------------------------------------------------------------


def _with_timeout[T](timeout_seconds: float, fn: Callable[[], T]) -> T:
    """Run a parse with a hard wall-clock deadline.

    Parsers run in a daemon thread so a pathological file can never block the
    request handler; ``join`` enforces the deadline and the request returns
    ``ai_attachment_parse_timeout``. All working data is in-memory, so a
    timed-out thread leaves no temp resources behind.
    """
    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = fn()
        except Exception as error:  # noqa: BLE001 - sanitized below
            result["error"] = error

    thread = threading.Thread(target=runner, daemon=True, name="dlr-attachment-parse")
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise AttachmentError("ai_attachment_parse_timeout")
    error = result.get("error")
    if error is not None:
        if isinstance(error, AttachmentError):
            raise error
        raise AttachmentError("ai_attachment_parse_failed") from None
    return cast(T, result["value"])


@dataclass(frozen=True)
class NativeImage:
    """An image accepted for the Provider-native input path."""

    filename: str
    content_type: str
    category: str
    data_base64: str
    size_bytes: int


@dataclass(frozen=True)
class ParsedText:
    """Bounded server-side extracted text for the fallback path."""

    filename: str
    content_type: str
    category: str
    text: str
    truncated: bool
    size_bytes: int


AttachmentResult = NativeImage | ParsedText


def process_attachment(
    filename: object,
    content_type: object,
    data_base64: object,
    provider_images_native: bool,
    char_budget: int,
) -> AttachmentResult:
    """Validate, classify and prepare one attachment.

    ``char_budget`` is this file's share of the total parsed-text budget
    (allocated by the caller across all attachments). Images go the native
    route when the Provider capability table allows it; every other category
    is parsed server-side into bounded text.
    """
    safe_filename = sanitize_filename(filename)
    data = decode_data(data_base64)
    category, normalized_content_type = classify(safe_filename, content_type)
    _sniff(normalized_content_type, data)
    if category == IMAGE_CATEGORY:
        if not provider_images_native:
            raise AttachmentError("ai_attachment_image_unsupported")
        return NativeImage(
            filename=safe_filename,
            content_type=normalized_content_type,
            category=category,
            data_base64=cast(str, data_base64),
            size_bytes=len(data),
        )
    if category == PDF_CATEGORY:
        text, truncated = _with_timeout(
            PARSE_TIMEOUT_SECONDS, lambda: _parse_pdf(data, char_budget)
        )
    elif category == DOCX_CATEGORY:
        text, truncated = _with_timeout(
            PARSE_TIMEOUT_SECONDS, lambda: _parse_docx(data, char_budget)
        )
    elif category == XLS_CATEGORY:
        text, truncated = _with_timeout(
            PARSE_TIMEOUT_SECONDS, lambda: _parse_xls(data, char_budget)
        )
    elif category == XLSX_CATEGORY:
        text, truncated = _with_timeout(
            PARSE_TIMEOUT_SECONDS, lambda: _parse_xlsx(data, char_budget)
        )
    else:
        text, truncated = _with_timeout(
            PARSE_TIMEOUT_SECONDS, lambda: _parse_text(data, char_budget)
        )
    return ParsedText(
        filename=safe_filename,
        content_type=normalized_content_type,
        category=category,
        text=text,
        truncated=truncated,
        size_bytes=len(data),
    )
