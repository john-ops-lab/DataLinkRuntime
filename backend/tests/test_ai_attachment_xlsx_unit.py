"""Focused, database-free XLS/XLSX attachment parser contract tests."""

import base64
import io
import struct
import time
import zipfile
import zlib
from types import SimpleNamespace

import pytest

from dlr.control.ai import attachments
from dlr.control.ai.attachments import AttachmentError, ParsedText
from test_xls_fixture import build_xls

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_xlsx() -> bytes:
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'count="1" uniqueCount="1">'
        "<si><t>shared XLSX sentinel</t></si></sst>"
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>inline XLSX sentinel</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return buffer.getvalue()


def build_xlsx_with_worksheet(worksheet: str, **extra_parts: str) -> bytes:
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
        for name, value in extra_parts.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def forge_member_size_and_crc(data: bytes, member_name: str, declared_data: bytes) -> bytes:
    forged = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(forged)) as archive:
        info = archive.getinfo(member_name)
    checksum = zlib.crc32(declared_data) & 0xFFFFFFFF
    struct.pack_into("<L", forged, info.header_offset + 14, checksum)
    struct.pack_into("<L", forged, info.header_offset + 22, len(declared_data))

    position = 0
    while True:
        position = forged.find(zipfile.stringCentralDir, position)
        if position < 0:
            raise AssertionError(f"central directory entry not found: {member_name}")
        filename_length = struct.unpack_from("<H", forged, position + 28)[0]
        extra_length = struct.unpack_from("<H", forged, position + 30)[0]
        comment_length = struct.unpack_from("<H", forged, position + 32)[0]
        filename = bytes(forged[position + 46 : position + 46 + filename_length]).decode()
        if filename == member_name:
            struct.pack_into("<L", forged, position + 16, checksum)
            struct.pack_into("<L", forged, position + 24, len(declared_data))
            return bytes(forged)
        position += 46 + filename_length + extra_length + comment_length


def test_xlsx_is_classified_and_parsed_to_bounded_text() -> None:
    result = attachments.process_attachment(
        "report.xlsx",
        XLSX_MIME,
        base64.b64encode(build_xlsx()).decode("ascii"),
        provider_images_native=False,
        char_budget=4096,
    )

    assert isinstance(result, ParsedText)
    assert result.category == attachments.XLSX_CATEGORY
    assert "shared XLSX sentinel" in result.text
    assert "42" in result.text
    assert "inline XLSX sentinel" in result.text


def test_legacy_xls_is_classified_and_parsed_to_bounded_text() -> None:
    result = attachments.process_attachment(
        "report.xls",
        "application/vnd.ms-excel",
        base64.b64encode(build_xls()).decode("ascii"),
        provider_images_native=False,
        char_budget=4096,
    )

    assert isinstance(result, ParsedText)
    assert result.category == attachments.XLS_CATEGORY
    assert "Legacy XLS sentinel" in result.text
    assert "42.0" in result.text
    assert "Second row" in result.text


def test_xlsx_requires_workbook_and_worksheet_parts() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")

    with pytest.raises(AttachmentError, match="ai_attachment_parse_failed"):
        attachments.process_attachment(
            "broken.xlsx",
            XLSX_MIME,
            base64.b64encode(buffer.getvalue()).decode("ascii"),
            provider_images_native=False,
            char_budget=4096,
        )


def test_xlsx_uses_cached_formula_values_and_ignores_active_parts() -> None:
    worksheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1">'
        '<c r="A1"><f>EXTERNAL_SECRET()</f><v>42</v></c>'
        '<c r="B1" t="b"><v>1</v></c>'
        '<c r="C1" t="inlineStr"><is><t>safe text</t></is></c>'
        "</row></sheetData></worksheet>"
    )
    data = build_xlsx_with_worksheet(
        worksheet,
        **{
            "xl/externalLinks/externalLink1.xml": "https://example.invalid/SHOULD_NOT_FETCH",
            "xl/vbaProject.bin": "MACRO_SECRET",
        },
    )

    result = attachments.process_attachment(
        "active.xlsx",
        XLSX_MIME,
        base64.b64encode(data).decode("ascii"),
        provider_images_native=False,
        char_budget=4096,
    )

    assert isinstance(result, ParsedText)
    assert "42\tTRUE\tsafe text" in result.text
    assert "EXTERNAL_SECRET" not in result.text
    assert "SHOULD_NOT_FETCH" not in result.text
    assert "MACRO_SECRET" not in result.text


@pytest.mark.parametrize(
    ("worksheet", "error_code"),
    [
        (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData/></worksheet>",
            "ai_attachment_no_text",
        ),
        ("<worksheet><sheetData>", "ai_attachment_parse_failed"),
    ],
)
def test_xlsx_empty_and_corrupt_workbooks_use_stable_errors(
    worksheet: str,
    error_code: str,
) -> None:
    with pytest.raises(AttachmentError) as caught:
        attachments.process_attachment(
            "edge.xlsx",
            XLSX_MIME,
            base64.b64encode(build_xlsx_with_worksheet(worksheet)).decode("ascii"),
            provider_images_native=False,
            char_budget=4096,
        )
    assert caught.value.code == error_code


def test_xlsx_budget_truncation_and_zip_bomb_are_bounded() -> None:
    worksheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>'
        + "bounded-cell-" * 20
        + "</t></is></c></row></sheetData></worksheet>"
    )
    result = attachments.process_attachment(
        "bounded.xlsx",
        XLSX_MIME,
        base64.b64encode(build_xlsx_with_worksheet(worksheet)).decode("ascii"),
        provider_images_native=False,
        char_budget=40,
    )
    assert isinstance(result, ParsedText)
    assert result.truncated is True
    assert len(result.text) == 40

    with pytest.raises(AttachmentError) as caught:
        attachments.process_attachment(
            "bomb.xlsx",
            XLSX_MIME,
            base64.b64encode(
                build_xlsx_with_worksheet(worksheet, **{"xl/sharedStrings.xml": "A" * 2_000_000})
            ).decode("ascii"),
            provider_images_native=False,
            char_budget=4096,
        )
    assert caught.value.code == "ai_attachment_unsafe_archive"


def test_xlsx_rejects_forged_short_size_and_crc_metadata() -> None:
    worksheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>'
    )
    declared_shared_strings = (
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        b'count="1" uniqueCount="1"><si><t>forged sentinel</t></si></sst>'
    )
    actual_shared_strings = declared_shared_strings + b" " * (attachments.MAX_DOCX_MEMBER_BYTES + 1)
    data = build_xlsx_with_worksheet(
        worksheet,
        **{"xl/sharedStrings.xml": actual_shared_strings.decode()},
    )
    forged = forge_member_size_and_crc(
        data,
        "xl/sharedStrings.xml",
        declared_shared_strings,
    )
    with zipfile.ZipFile(io.BytesIO(forged)) as archive:
        # Demonstrate the adversarial premise: stdlib ZipFile trusts the
        # forged metadata and exposes only the valid short XML prefix.
        assert archive.read("xl/sharedStrings.xml") == declared_shared_strings

    with pytest.raises(AttachmentError) as caught:
        attachments.process_attachment(
            "forged.xlsx",
            XLSX_MIME,
            base64.b64encode(forged).decode("ascii"),
            provider_images_native=False,
            char_budget=4096,
        )
    assert caught.value.code == "ai_attachment_unsafe_archive"


def test_xls_dimension_truncation_releases_parser_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = False

    class FakeWorkbook:
        nsheets = 1

        def sheet_by_index(self, _index: int) -> SimpleNamespace:
            rows = [
                [
                    SimpleNamespace(ctype=attachments.xlrd.XL_CELL_TEXT, value="first"),
                    SimpleNamespace(ctype=attachments.xlrd.XL_CELL_TEXT, value="extra"),
                ],
                [SimpleNamespace(ctype=attachments.xlrd.XL_CELL_TEXT, value="second")],
            ]
            return SimpleNamespace(get_rows=lambda: iter(rows))

        def release_resources(self) -> None:
            nonlocal released
            released = True

    monkeypatch.setattr(attachments.xlrd, "open_workbook", lambda **_kwargs: FakeWorkbook())
    monkeypatch.setattr(attachments, "MAX_XLS_ROWS", 1)
    monkeypatch.setattr(attachments, "MAX_XLS_COLUMNS", 1)

    text, truncated = attachments._parse_xls(b"fixture", 4096)
    assert text.startswith("first")
    assert attachments.TRUNCATION_MARKER in text
    assert truncated is True
    assert released is True


def test_xlsx_parse_timeout_uses_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attachments, "PARSE_TIMEOUT_SECONDS", 0.01)

    def slow_parse(_data: bytes, _limit: int) -> tuple[str, bool]:
        time.sleep(0.2)
        return "late", False

    monkeypatch.setattr(attachments, "_parse_xlsx", slow_parse)
    with pytest.raises(AttachmentError) as caught:
        attachments.process_attachment(
            "slow.xlsx",
            XLSX_MIME,
            base64.b64encode(build_xlsx()).decode("ascii"),
            provider_images_native=False,
            char_budget=4096,
        )
    assert caught.value.code == "ai_attachment_parse_timeout"
