"""The shared scoped reading contract keeps binary media outside tool JSON."""

import base64
import hashlib

import pytest

from dlr.control.ai.attachment_reading import (
    AttachmentMedia,
    sheet_range_remainder,
    validate_sheet_range,
)


def test_media_hash_body_and_dimensions_are_checked() -> None:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    kwargs = dict(
        content_type="image/png",
        data_base64=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        attachment_ref="reference-1",
        width=1,
        height=1,
        page=3,
    )
    media = AttachmentMedia(**kwargs)
    assert media.source()["page"] == 3
    assert media.data_base64 not in repr(media)
    with pytest.raises(ValueError, match="ai_attachment_media_invalid"):
        AttachmentMedia(**{**kwargs, "sha256": "0" * 64})


@pytest.mark.parametrize("range_a1", ["A1:B100", "A1", "XFD1048576"])
def test_bounded_sheet_ranges(range_a1: str) -> None:
    validate_sheet_range(range_a1)


@pytest.mark.parametrize("range_a1", ["A1:B101", "A0", "B2:A1", "../file", "XFE1"])
def test_invalid_sheet_ranges_are_rejected(range_a1: str) -> None:
    with pytest.raises(ValueError):
        validate_sheet_range(range_a1)


@pytest.mark.parametrize("range_a1", ["A1:D30", "Z99:AC108", "XFD1048377:XFD1048576", "B3"])
def test_every_prefix_remainder_covers_exactly_the_unreturned_cells(range_a1: str) -> None:
    from openpyxl.utils.cell import cols_from_range, rows_from_range

    original = [cell for row in rows_from_range(range_a1) for cell in row]
    for returned in range(len(original) + 1):
        ranges = sheet_range_remainder(range_a1, returned)
        assert len(ranges) <= 2
        remaining = [cell for part in ranges for row in rows_from_range(part) for cell in row]
        assert remaining == original[returned:]
        assert not set(original[:returned]).intersection(remaining)
        for part in ranges:
            validate_sheet_range(part)
            assert len(list(cols_from_range(part))) >= 1
    with pytest.raises(ValueError):
        sheet_range_remainder(range_a1, len(original) + 1)
    with pytest.raises(ValueError):
        sheet_range_remainder(range_a1, -1)
