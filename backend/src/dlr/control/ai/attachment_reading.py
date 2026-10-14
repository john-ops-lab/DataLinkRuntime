"""Request-scoped attachment reading and non-text media transport contracts.

Binary bodies stay in frozen internal objects, never ordinary tool JSON,
user-visible summaries or audit metadata. Storage owns references and pins.
"""

import base64
import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

MAX_ATTACHMENT_MEDIA_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_PIXELS = 16_000_000
MAX_SHEET_CELLS = 200
_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_RANGE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?$")


def validate_attachment_reference(value: str) -> None:
    if not _REFERENCE.fullmatch(value):
        raise ValueError("ai_attachment_reference_invalid")


def validate_sheet_range(value: str) -> None:
    match = _RANGE.fullmatch(value)
    if match is None:
        raise ValueError("ai_attachment_range_invalid")

    def column(name: str) -> int:
        result = 0
        for char in name:
            result = result * 26 + ord(char) - ord("A") + 1
        return result

    left, top = column(match[1]), int(match[2])
    right, bottom = column(match[3] or match[1]), int(match[4] or match[2])
    if right < left or bottom < top or right > 16384 or bottom > 1048576:
        raise ValueError("ai_attachment_range_invalid")
    if (right - left + 1) * (bottom - top + 1) > MAX_SHEET_CELLS:
        raise ValueError("ai_attachment_range_invalid")


@dataclass(frozen=True)
class AttachmentMedia:
    content_type: str
    data_base64: str = field(repr=False)
    sha256: str
    size_bytes: int
    attachment_ref: str
    width: int | None = None
    height: int | None = None
    page_count: int | None = None
    page: int | None = None
    sheet: str | None = None
    range_a1: str | None = None

    def __post_init__(self) -> None:
        validate_attachment_reference(self.attachment_ref)
        if self.content_type not in {"image/png", "image/jpeg", "image/webp", "application/pdf"}:
            raise ValueError("ai_attachment_media_invalid")
        if not 0 < self.size_bytes <= MAX_ATTACHMENT_MEDIA_BYTES:
            raise ValueError("ai_attachment_media_invalid")
        if len(self.data_base64) > (MAX_ATTACHMENT_MEDIA_BYTES + 2) // 3 * 4:
            raise ValueError("ai_attachment_media_invalid")
        try:
            raw = base64.b64decode(self.data_base64, validate=True)
        except (ValueError, UnicodeError):
            raise ValueError("ai_attachment_media_invalid") from None
        if len(raw) != self.size_bytes or hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("ai_attachment_media_invalid")
        magic = (
            raw.startswith(b"\x89PNG\r\n\x1a\n")
            if self.content_type == "image/png"
            else raw.startswith(b"\xff\xd8\xff")
            if self.content_type == "image/jpeg"
            else raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
            if self.content_type == "image/webp"
            else raw.startswith(b"%PDF-")
        )
        if not magic:
            raise ValueError("ai_attachment_media_invalid")
        if self.content_type.startswith("image/"):
            if (
                self.width is None
                or self.height is None
                or self.width <= 0
                or self.height <= 0
                or self.width * self.height > MAX_ATTACHMENT_PIXELS
            ):
                raise ValueError("ai_attachment_media_invalid")
        elif self.page_count is None or not 0 < self.page_count <= 1000:
            raise ValueError("ai_attachment_media_invalid")
        if self.page is not None and self.page <= 0:
            raise ValueError("ai_attachment_media_invalid")
        if self.range_a1 is not None:
            validate_sheet_range(self.range_a1)

    def source(self) -> dict[str, object]:
        return {
            "attachment_ref": self.attachment_ref,
            "sha256": self.sha256,
            **({"page": self.page} if self.page is not None else {}),
            **({"sheet": self.sheet} if self.sheet is not None else {}),
            **({"range": self.range_a1} if self.range_a1 is not None else {}),
        }


@dataclass(frozen=True)
class AttachmentReadResult:
    data: dict[str, object]
    media: tuple[AttachmentMedia, ...] = ()


class ScopedAttachmentReader(Protocol):
    """Every operation revalidates the frozen refs, hashes and representation revisions.

    Implementations own Principal/Adapter/scope authorization, active pins,
    original hash checks and actually stopping timed-out conversion processes.
    They must not return any material outside this request's reference list.
    """

    def overview(self) -> dict[str, object]: ...

    def read_page(
        self, attachment_ref: str, page: int, view: str, *, timeout_seconds: float
    ) -> AttachmentReadResult: ...

    def read_sheet(
        self, attachment_ref: str, sheet: str, range_a1: str, *, timeout_seconds: float
    ) -> AttachmentReadResult: ...
