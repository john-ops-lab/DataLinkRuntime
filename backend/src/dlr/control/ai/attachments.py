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
- PDF / DOCX / text / code are parsed server-side into bounded UTF-8 text
  that joins the request context. DOCX uses only the stdlib (``zipfile`` +
  ``xml.etree``); PDF uses the pinned, pure-Python ``pypdf`` (BSD-3-Clause,
  no transitive deps, headless-container friendly).
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from pypdf import PdfReader
from pypdf.errors import PdfReadError

# ---------------------------------------------------------------------------
# Bounded limits (the single source of truth for the capability endpoint).
# ---------------------------------------------------------------------------

MAX_ATTACHMENTS = 8
MAX_FILE_BYTES = 6 * 1024 * 1024  # per-file decoded size
MAX_TOTAL_BYTES = 12 * 1024 * 1024  # sum of decoded sizes
MAX_PARSED_CHARS_PER_FILE = 64 * 1024  # extracted text budget per file
MAX_PARSED_TOTAL_CHARS = 256 * 1024  # total extracted text budget
PARSE_TIMEOUT_SECONDS = 30.0

# DOCX (zip) decompression-bomb guards: no single member may inflate past the
# member cap, the archive's total uncompressed size stays below the total cap
# and the inflation ratio stays bounded.
MAX_DOCX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_DOCX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_DOCX_INFLATION_RATIO = 50

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
TEXT_CATEGORY = "text"
CODE_CATEGORY = "code"

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
    return _truncate("".join(chunks), limit_chars)


def _check_archive_safety(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    uncompressed_total = 0
    compressed_total = 0
    for info in infos:
        name = info.filename
        if not name or name.startswith("/") or "\\" in name or ".." in name:
            raise AttachmentError("ai_attachment_unsafe_archive")
        if info.file_size > MAX_DOCX_MEMBER_BYTES:
            raise AttachmentError("ai_attachment_unsafe_archive")
        uncompressed_total += info.file_size
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
    _check_archive_safety(archive)
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
    if not chunks:
        raise AttachmentError("ai_attachment_no_text")
    return _truncate("".join(chunks), limit_chars)


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
