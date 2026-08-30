"""M5.7 Wave B2 attachment backend contract tests (all provider traffic fake).

Covers: accepted text/code/PDF/DOCX/XLS/XLSX/image paths; provider-native capability
gating (true/false); bounded server-side fallback parsing; scanned-PDF
actionable error; fake MIME / oversize / count / total / parse-length /
timeout / corrupt-file rejections; archive-safety; filename injection;
request-only lifecycle (no DB rows, no temp files, no log/response echo of
attachment content or credentials); candidate=null and normal AiModelOutput
with attachments; backward compatibility of attachment-free requests; and
the per-Provider capability table.
"""

import base64
import io
import json
import os
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.ai import attachments as attachments_module
from dlr.control.ai import providers
from dlr.control.ai.attachments import AttachmentError
from dlr.control.schemas.ai import AiSettingDraft
from test_ai import (
    assist_body,
    configure,
    create_adapter,
    fake_chat_response,
    setting_payload,
    valid_output,
)
from test_xls_fixture import build_xls

ATTACH_SENTINEL = "attachment-plaintext-sentinel-7f3a"
CREDENTIAL_SENTINEL = "attachment-credential-plaintext-sentinel"
PROVIDER_KEY_SENTINEL = "attachment-provider-key-plaintext-sentinel"

PNG_1PX_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
PNG_1PX = base64.b64decode(PNG_1PX_BASE64)

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLS_MIME = "application/vnd.ms-excel"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def attachment(filename: str, content_type: str, data: bytes) -> dict[str, str]:
    return {
        "filename": filename,
        "content_type": content_type,
        "data_base64": b64(data),
    }


def build_pdf(text: str, *, trailing: bool = True) -> bytes:
    """Build a minimal valid single-page PDF carrying literal text.

    The xref offsets are computed so pypdf parses the file without repair.
    Literal text must not contain parentheses/backslashes (test texts keep to
    plain ASCII words); ``trailing=False`` yields an empty-content page used
    to emulate a scanned (text-less) PDF.
    """
    stream = b""
    if trailing:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")

    def obj(number: int, body: bytes) -> bytes:
        return b"%d 0 obj\n" % number + body + b"\nendobj\n"

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    header = b"%PDF-1.4\n"
    chunks: list[bytes] = [header]
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(b"".join(chunks)))
        chunks.append(obj(index, body))
    xref_position = len(b"".join(chunks))
    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        xref += f"{offset:010d} 00000 n \n".encode("ascii")
    trailer = (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_position).encode()
        + b"\n%%EOF\n"
    )
    return b"".join(chunks) + xref + trailer


def build_docx(paragraphs: list[str]) -> bytes:
    """Build a minimal valid DOCX (zip + word/document.xml)."""
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_WORD_NS}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def build_xlsx() -> bytes:
    """Build a minimal XLSX with shared, inline and numeric cell values."""
    workbook = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<workbook xmlns="{_XLSX_NS}"><sheets>'
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
        "</sheets></workbook>"
    )
    shared_strings = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sst xmlns="{_XLSX_NS}" count="1" uniqueCount="1">'
        "<si><t>shared XLSX attachment sentinel</t></si></sst>"
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<worksheet xmlns="{_XLSX_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>inline XLSX value</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/sharedStrings.xml", shared_strings)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return buffer.getvalue()


def captured_payload(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any]]:
    captured: dict[str, dict[str, Any]] = {}

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        captured["payload"] = payload or {}
        return fake_chat_response(valid_output())

    monkeypatch.setattr(providers, "_request_json", fake_request)
    return captured


def system_prompt(captured: dict[str, dict[str, Any]]) -> str:
    payload = captured["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list) and isinstance(messages[0], dict)
    prompt = messages[0]["content"]
    assert isinstance(prompt, str)
    return prompt


def user_message(captured: dict[str, dict[str, Any]]) -> object:
    payload = captured["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list)
    last = messages[-1]
    assert isinstance(last, dict)
    return last["content"]


def assert_stable_error(response: Any, status: int, code: str, *message_fragments: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["detail"]["code"] == code, body
    message = body["detail"]["message"]
    for fragment in message_fragments:
        assert fragment in message, message


# ---------------------------------------------------------------------------
# Capability contract
# ---------------------------------------------------------------------------


def test_attachment_capabilities_endpoint_exposes_stable_contract(
    api_client: TestClient,
) -> None:
    response = api_client.get("/api/ai/attachment-capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limits"] == {
        "max_attachments": attachments_module.MAX_ATTACHMENTS,
        "max_file_bytes": attachments_module.MAX_FILE_BYTES,
        "max_total_bytes": attachments_module.MAX_TOTAL_BYTES,
        "max_parsed_chars_per_file": attachments_module.MAX_PARSED_CHARS_PER_FILE,
        "max_parsed_total_chars": attachments_module.MAX_PARSED_TOTAL_CHARS,
        "parse_timeout_seconds": attachments_module.PARSE_TIMEOUT_SECONDS,
    }
    assert body["supported_content_types"] == sorted(attachments_module.MIME_EXTENSIONS)
    assert XLS_MIME in body["supported_content_types"]
    assert XLSX_MIME in body["supported_content_types"]
    assert attachments_module.MAX_TOTAL_BYTES == (
        attachments_module.MAX_ATTACHMENTS * attachments_module.MAX_FILE_BYTES
    )
    by_provider = {item["provider"]: item for item in body["providers"]}
    assert set(by_provider) == {
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "qwen",
        "kimi",
        "minimax",
        "glm",
        "doubao",
        "hunyuan",
        "openrouter",
        "siliconflow",
        "ollama",
        "custom_openai_compatible",
    }
    # Capability is explicit, never inferred from a model name or URL. Native
    # multimodal input is guaranteed only by the OpenAI, Anthropic and Gemini
    # protocol adapters in this build; other OpenAI-compatible entries fall
    # back to bounded server-side parsing unless a custom profile opts in.
    for name in ("openai", "anthropic", "gemini"):
        assert by_provider[name]["images_native"] is True
        assert by_provider[name]["files_native"] is False
    for name in (
        "deepseek",
        "qwen",
        "kimi",
        "minimax",
        "glm",
        "doubao",
        "hunyuan",
        "openrouter",
        "siliconflow",
        "ollama",
        "custom_openai_compatible",
    ):
        assert by_provider[name]["images_native"] is False
        assert by_provider[name]["files_native"] is False


# ---------------------------------------------------------------------------
# Happy paths: text / code / PDF / DOCX / XLS / XLSX fallback parsing
# ---------------------------------------------------------------------------


def test_assist_text_attachment_parsed_into_bounded_context(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-text")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    body = assist_body()
    body["attachments"] = [attachment("notes.txt", "text/plain", ATTACH_SENTINEL.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["candidate"] is not None
    assert ATTACH_SENTINEL not in response.text

    prompt = system_prompt(captured)
    assert '\\"attachments\\"' in json.dumps(captured["payload"])
    assert ATTACH_SENTINEL in prompt
    assert '"filename": "notes.txt"' in prompt
    assert '"content_type": "text/plain"' in prompt
    assert '"category": "text"' in prompt
    assert '"truncated": false' in prompt
    # The user message stays a plain string (no native parts on this path).
    assert user_message(captured) == body["message"]


def test_assist_code_attachment_via_octet_stream(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-code")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    code = "def handle(context, input):\n    return {'attached': True}\n"
    body = assist_body()
    body["attachments"] = [attachment("adapter.py", "application/octet-stream", code.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert "adapter.py" in prompt
    assert "def handle(context, input):" in prompt
    assert '"category": "text"' in prompt


def test_assist_pdf_attachment_fallback_parse(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-pdf")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    pdf = build_pdf("PDF attachment sentinel report")
    body = assist_body()
    body["attachments"] = [attachment("report.pdf", "application/pdf", pdf)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert "PDF attachment sentinel report" in prompt
    assert '"category": "pdf"' in prompt


def test_assist_docx_attachment_fallback_parse(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-docx")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    docx = build_docx(["first docx paragraph", "second docx paragraph"])
    body = assist_body()
    body["attachments"] = [
        attachment(
            "notes.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            docx,
        )
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert "first docx paragraph" in prompt
    assert "second docx paragraph" in prompt
    assert '"category": "docx"' in prompt


def test_assist_xlsx_attachment_fallback_parse(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-xlsx")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    body = assist_body()
    body["attachments"] = [attachment("report.xlsx", XLSX_MIME, build_xlsx())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert "shared XLSX attachment sentinel" in prompt
    assert "inline XLSX value" in prompt
    assert "42" in prompt
    assert '"category": "xlsx"' in prompt


def test_assist_xls_attachment_fallback_parse(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-xls")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    body = assist_body()
    body["attachments"] = [attachment("report.xls", XLS_MIME, build_xls())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert "Legacy XLS sentinel" in prompt
    assert "Second row" in prompt
    assert '"category": "xls"' in prompt
    assert "Spreadsheet attachments (XLS and XLSX)" in prompt


def test_assist_attachments_coexist_with_context_snippets(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-mixed")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    body = assist_body()
    body["context_snippets"] = [
        {"source": "code", "text": "selected code line", "start_line": 1, "end_line": 1}
    ]
    body["attachments"] = [attachment("a.txt", "text/plain", ATTACH_SENTINEL.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert "selected code line" in prompt
    assert ATTACH_SENTINEL in prompt


# ---------------------------------------------------------------------------
# Provider-native image capability
# ---------------------------------------------------------------------------


def test_assist_image_native_payload_for_openai(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-image-native")
    configure(api_client, provider="openai")
    captured = captured_payload(monkeypatch)
    body = assist_body()
    body["attachments"] = [
        attachment("photo.png", "image/png", PNG_1PX),
        attachment("logo.jpeg", "image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 32),
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    # Native images never join the parsed-text context and never echo back.
    assert ATTACH_SENTINEL not in response.text
    assert '\\"attachments\\"' not in json.dumps(captured["payload"])

    content = user_message(captured)
    assert isinstance(content, list) and len(content) == 3
    assert content[0] == {"type": "text", "text": body["message"]}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_1PX_BASE64}"
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_assist_image_unsupported_without_vision_capability(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-image-blocked")
    configure(api_client)
    calls: list[object] = []

    def failing_request(*_: object, **__: object) -> object:
        calls.append(1)
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(providers, "_request_json", failing_request)
    body = assist_body()
    body["attachments"] = [attachment("photo.png", "image/png", PNG_1PX)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(
        response,
        422,
        "ai_attachment_image_unsupported",
        "更换支持图片的模型",
        "OCR",
    )
    assert calls == []
    # No fake-OCR path exists: the Provider never sees the image.
    assert PNG_1PX_BASE64 not in response.text


@pytest.mark.parametrize("provider", ["deepseek", "kimi", "minimax", "custom_openai_compatible"])
def test_assist_image_rejected_for_every_provider_without_table_entry(
    api_client: TestClient, provider: str
) -> None:
    adapter = create_adapter(api_client, f"attach-image-{provider}")
    configure(api_client, provider=provider)
    body = assist_body()
    body["attachments"] = [attachment("photo.png", "image/png", PNG_1PX)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_image_unsupported", "更换支持图片的模型")


def test_provider_rejecting_native_image_maps_to_stable_code() -> None:
    """A Provider that answers 400/422 to a native image payload yields the
    actionable image code instead of a generic transport error."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"error": {"message": "image rejected"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with pytest.raises(providers.AiProviderError) as exc_info:
            providers.chat(
                AiSettingDraft.model_validate(
                    setting_payload(base_url=f"http://127.0.0.1:{server.server_port}")
                ),
                None,
                [{"role": "user", "content": "hi"}],
                structured=False,
                image_input=True,
            )
    finally:
        server.shutdown()
        server.server_close()
    assert exc_info.value.code == "ai_attachment_image_unsupported"


def test_provider_image_error_never_leaks_image_bytes_or_echoes() -> None:
    """The sanitized mapping keeps the stable code and never reflects any
    Provider-supplied body back to the browser."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"error": {"message": "echo " + PNG_1PX_BASE64}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format_string: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with pytest.raises(providers.AiProviderError) as exc_info:
            providers.chat(
                AiSettingDraft.model_validate(
                    setting_payload(base_url=f"http://127.0.0.1:{server.server_port}")
                ),
                None,
                [{"role": "user", "content": "hi"}],
                structured=False,
                image_input=True,
            )
    finally:
        server.shutdown()
        server.server_close()
    assert exc_info.value.code == "ai_attachment_image_unsupported"


# ---------------------------------------------------------------------------
# Type / filename / size / count rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        # MIME says PDF but the body is text and the extension is .txt.
        ("notes.txt", "application/pdf", ATTACH_SENTINEL.encode()),
        # Extension and MIME disagree.
        ("report.png", "application/pdf", build_pdf("real pdf")),
        ("photo.txt", "image/png", PNG_1PX),
        # Declared image types whose magic bytes do not match.
        ("fake.png", "image/png", b"not a png at all"),
        ("fake.jpg", "image/jpeg", b"\x00\x00\x00\x00jpeg"),
        ("fake.webp", "image/webp", b"RIFF1234NOPE1234"),
        ("fake.pdf", "application/pdf", b"not a pdf"),
        (
            "fake.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"not a docx",
        ),
        # Legacy Word remains unsupported; legacy Excel is covered by the
        # positive XLS parser test above.
        ("old.doc", "application/msword", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ("blob.bin", "application/x-unknown-type", b"\x00" * 16),
    ],
)
def test_assist_rejects_unknown_or_fake_types_with_stable_code(
    api_client: TestClient,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    adapter = create_adapter(api_client, "attach-type-reject")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [attachment(filename, content_type, data)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_type_unsupported")
    # No filename or content echo in the error.
    assert filename not in response.text
    assert content_type not in response.text


@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd",
        "/absolute/path.txt",
        "sub\\dir\\file.txt",
        "..",
        ".hidden",
        "a\nb.txt",
    ],
)
def test_assist_rejects_filename_injection(api_client: TestClient, filename: str) -> None:
    adapter = create_adapter(api_client, "attach-name-reject")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [attachment(filename, "text/plain", ATTACH_SENTINEL.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_filename_invalid")
    assert filename not in response.text


def test_assist_rejects_invalid_base64_without_echo(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, "attach-base64-reject")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [
        {"filename": "x.txt", "content_type": "text/plain", "data_base64": "%%%not-base64%%%"}
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_invalid")
    assert "not-base64" not in response.text


def test_assist_rejects_blank_attachment_fields_without_echo(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, "attach-blank-reject")
    configure(api_client)
    for overrides in (
        {"filename": "   "},
        {"content_type": ""},
        {"data_base64": "   "},
    ):
        body = assist_body()
        entry = {"filename": "x.txt", "content_type": "text/plain", "data_base64": "aGk="}
        entry.update(overrides)
        body["attachments"] = [entry]
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
        assert_stable_error(response, 422, "ai_attachment_invalid")


@pytest.mark.parametrize(
    "entry",
    [
        # Missing one of the three required keys.
        {"content_type": "text/plain", "data_base64": "aGk="},
        {"filename": "a.txt", "data_base64": "aGk="},
        {"filename": "a.txt", "content_type": "text/plain"},
        # Non-object entries.
        "not-an-object",
        42,
        None,
        ["a.txt"],
    ],
)
def test_assist_rejects_missing_or_non_object_attachment_entries_with_stable_code(
    api_client: TestClient, entry: object
) -> None:
    """A malformed entry must never bypass the stable sanitized error: the
    response keeps the {code, message} detail shape and never echoes the
    offending input (including the base64 body)."""
    adapter = create_adapter(api_client, "attach-malformed-reject")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [entry]  # type: ignore[list-item]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_invalid")
    detail = response.json()["detail"]
    assert isinstance(detail, dict) and "code" in detail
    assert "aGk=" not in response.text
    assert "data_base64" not in response.text


def test_assist_rejects_per_file_size_limit(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, "attach-size-reject")
    configure(api_client)
    oversized = b"x" * (attachments_module.MAX_FILE_BYTES + 1)
    body = assist_body()
    body["attachments"] = [attachment("big.txt", "text/plain", oversized)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_too_large")


def test_assist_rejects_total_size_limit(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, "attach-total-reject")
    configure(api_client)
    monkeypatch.setattr(attachments_module, "MAX_TOTAL_BYTES", 12 * 1024 * 1024)
    # Three files below the per-file cap still exceed the total cap.
    chunk = b"x" * (attachments_module.MAX_TOTAL_BYTES // 3 + 1024)
    body = assist_body()
    body["attachments"] = [attachment(f"part{i}.txt", "text/plain", chunk) for i in range(3)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_total_too_large")


def test_assist_rejects_attachment_count_limit(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, "attach-count-reject")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [
        attachment(f"f{i}.txt", "text/plain", b"small")
        for i in range(attachments_module.MAX_ATTACHMENTS + 1)
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_count_exceeded")


# ---------------------------------------------------------------------------
# Parse bounds: length, scanned PDF, corrupt files, timeout, archive safety
# ---------------------------------------------------------------------------


def test_assist_parsed_text_is_bounded_and_marked_truncated(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-truncate")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    long_text = "word " * (attachments_module.MAX_PARSED_CHARS_PER_FILE)
    body = assist_body()
    body["attachments"] = [attachment("long.txt", "text/plain", long_text.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    prompt = system_prompt(captured)
    assert '"truncated": true' in prompt
    assert "attachment text truncated by DLR context bound" in prompt
    # The context never carries more than the per-file cap plus the marker.
    context = json.loads(
        prompt[prompt.index("Current Adapter context:") + len("Current Adapter context:") :]
    )
    attachments = context["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["truncated"] is True
    assert len(attachments[0]["text"]) <= attachments_module.MAX_PARSED_CHARS_PER_FILE + len(
        attachments_module.TRUNCATION_MARKER
    )


def test_total_parsed_budget_is_shared_across_attachments() -> None:
    """Unit-level: each attachment receives an equal share of the total
    parsed-text budget, capped by the per-file cap, deterministically."""
    budget = attachments_module.MAX_PARSED_TOTAL_CHARS // 4
    for _ in range(2):
        result = attachments_module.process_attachment(
            "a.txt", "text/plain", b64(b"y" * (budget + 1000)), False, budget
        )
        assert isinstance(result, attachments_module.ParsedText)
        assert result.truncated is True
        assert len(result.text) <= budget + len(attachments_module.TRUNCATION_MARKER)


def build_docx_empty_paragraphs(count: int) -> bytes:
    """Build a DOCX whose body contains paragraphs without any w:t runs."""
    body = "<w:p/>" * count
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_WORD_NS}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        # Scanned PDF: the page has no content stream at all.
        ("scan.pdf", "application/pdf", build_pdf("ignored", trailing=False)),
        # PDF whose only page text is whitespace.
        ("blank.pdf", "application/pdf", build_pdf("   ")),
        # DOCX whose paragraphs carry no w:t runs (only paragraph breaks).
        (
            "empty.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            build_docx_empty_paragraphs(3),
        ),
        # Whitespace-only text files carry no extractable content.
        ("blank.txt", "text/plain", b"   \n  \n"),
    ],
)
def test_textless_attachments_return_actionable_no_text_error_across_categories(
    api_client: TestClient,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    """The no-text contract is consistent: scanned PDFs, whitespace-only
    pages, DOCX without w:t text and blank text files all yield the same
    stable actionable error instead of whitespace-only context."""
    adapter = create_adapter(api_client, "attach-textless")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [attachment(filename, content_type, data)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(
        response,
        422,
        "ai_attachment_no_text",
        "文本层",
        "扫描件",
    )


def _docx_without_document_part() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/other.xml", "<x/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        # PDF magic present but the body is corrupt.
        ("broken.pdf", "application/pdf", b"%PDF-1.4\nbroken beyond repair"),
        # DOCX zip magic present but the archive is truncated.
        (
            "broken.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04" + b"\x00" * 64,
        ),
        # DOCX without the required document part.
        (
            "empty.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _docx_without_document_part(),
        ),
        # XLS magic is present but the OLE container is incomplete.
        ("broken.xls", XLS_MIME, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ],
)
def test_assist_rejects_corrupt_files_with_stable_parse_error(
    api_client: TestClient,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    adapter = create_adapter(api_client, "attach-corrupt")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [attachment(filename, content_type, data)]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 422, "ai_attachment_parse_failed")


def test_docx_zip_bomb_and_unsafe_members_rejected() -> None:
    # High-inflation archive (heavily compressible content) trips the ratio cap.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "A" * (2 * 1024 * 1024))
    with pytest.raises(AttachmentError) as exc:
        attachments_module.process_attachment(
            "bomb.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b64(buffer.getvalue()),
            False,
            1024,
        )
    assert exc.value.code == "ai_attachment_unsafe_archive"

    # Path-traversal member names are rejected before any read.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../evil.txt", "x")
    with pytest.raises(AttachmentError) as exc:
        attachments_module.process_attachment(
            "evil.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b64(buffer.getvalue()),
            False,
            1024,
        )
    assert exc.value.code == "ai_attachment_unsafe_archive"


def test_parse_timeout_returns_stable_code_and_no_partial_context(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-timeout")
    configure(api_client)
    monkeypatch.setattr(attachments_module, "PARSE_TIMEOUT_SECONDS", 0.05)

    def slow_parse(_data: bytes, _limit: int) -> tuple[str, bool]:
        time.sleep(1.0)
        return "late text", False

    monkeypatch.setattr(attachments_module, "_parse_text", slow_parse)
    body = assist_body()
    body["attachments"] = [attachment("slow.txt", "text/plain", b"payload")]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert_stable_error(response, 504, "ai_attachment_parse_timeout")
    assert "late text" not in response.text


# ---------------------------------------------------------------------------
# Privacy / lifecycle: no persistence, no logs, no response echo
# ---------------------------------------------------------------------------


def test_attachments_leave_no_db_rows_logs_temp_files_or_response_echo(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = create_adapter(api_client, "attach-privacy")
    configure(api_client)
    secret_text = CREDENTIAL_SENTINEL + " " + PROVIDER_KEY_SENTINEL
    captured = captured_payload(monkeypatch)
    temp_dir = tempfile.gettempdir()
    baseline = time.time()

    def row_counts() -> dict[str, int]:
        with session_factory() as session:
            counts: dict[str, int] = {}
            for table in ("adapters", "adapter_versions", "executions", "credentials"):
                counts[table] = session.scalar(text(f"SELECT count(*) FROM {table}"))
        return counts

    before = row_counts()
    body = assist_body()
    body["attachments"] = [attachment("private.txt", "text/plain", secret_text.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    # Attachment content never reaches the browser response.
    assert secret_text not in response.text
    assert CREDENTIAL_SENTINEL not in response.text
    assert PROVIDER_KEY_SENTINEL not in response.text
    # ... but it did reach the Provider by design (the bounded contract).
    prompt = system_prompt(captured)
    assert secret_text in prompt

    # Failed path too: corrupt attachment must not persist or leak either.
    failed_body = assist_body()
    failed_body["attachments"] = [attachment("broken.pdf", "application/pdf", b"%PDF-1.4\ngarbage")]
    failed = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=failed_body)
    assert_stable_error(failed, 422, "ai_attachment_parse_failed")
    assert "garbage" not in failed.text

    # No database rows were created by either request.
    assert row_counts() == before
    # No attachment content entered any log line.
    assert secret_text not in caplog.text
    assert CREDENTIAL_SENTINEL not in caplog.text
    assert PROVIDER_KEY_SENTINEL not in caplog.text
    # No temp files were created during the requests (everything in memory).
    for entry in os.scandir(temp_dir):
        if entry.is_file() and entry.stat().st_mtime >= baseline:
            try:
                with open(entry.path, "rb") as handle:
                    if secret_text.encode() in handle.read():
                        raise AssertionError(f"temp file leaked attachment content: {entry.path}")
            except OSError:
                continue


# ---------------------------------------------------------------------------
# AiModelOutput contract with attachments
# ---------------------------------------------------------------------------


def test_assist_attachments_can_answer_without_candidate(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-null-candidate")
    configure(api_client)

    def fake_request(
        _method: str,
        _url: str,
        _headers: dict[str, str],
        payload: dict[str, object] | None = None,
        **_: object,
    ) -> object:
        assert payload is not None and ATTACH_SENTINEL in json.dumps(payload)
        return fake_chat_response(
            {"message": "Attachment received; no code change needed.", "candidate": None}
        )

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body()
    body["attachments"] = [attachment("a.txt", "text/plain", ATTACH_SENTINEL.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["candidate"] is None
    assert result["message"] == "Attachment received; no code change needed."


def test_assist_attachments_keep_full_candidate_contract(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-candidate")
    configure(api_client)
    captured_payload(monkeypatch)
    body = assist_body()
    body["attachments"] = [attachment("a.txt", "text/plain", ATTACH_SENTINEL.encode())]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["candidate"] is not None
    assert result["candidate"]["code"].strip()
    assert result["provider"] == "custom_openai_compatible"
    # Attachment text never leaks into the Candidate or response.
    assert ATTACH_SENTINEL not in json.dumps(result)
    assert "attachments" not in json.dumps(result)


# ---------------------------------------------------------------------------
# Backward compatibility: attachment-free requests
# ---------------------------------------------------------------------------


def test_attachment_free_requests_keep_exact_previous_contract(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = create_adapter(api_client, "attach-compat")
    configure(api_client)
    captured = captured_payload(monkeypatch)
    body = assist_body()
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    # No attachments key in the Provider context; user message stays a plain
    # string; the system prompt keeps the exact pre-attachment shape.
    prompt = system_prompt(captured)
    assert '\\"attachments\\"' not in json.dumps(captured["payload"])
    assert "attachments array" not in prompt
    assert user_message(captured) == body["message"]
    # Explicit empty list behaves exactly like the omitted field.
    body["attachments"] = []
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text


def test_attachment_schema_keeps_strict_unknown_field_rejection(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, "attach-strict")
    configure(api_client)
    body = assist_body()
    body["attachments"] = [
        {"filename": "a.txt", "content_type": "text/plain", "data_base64": "aGk=", "extra": 1}
    ]
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 422
    assert "extra" not in response.text


# ---------------------------------------------------------------------------
# Different Provider capability regression
# ---------------------------------------------------------------------------


def test_provider_capability_table_matches_adapter_instances() -> None:
    assert providers.PROVIDERS["openai"].images_native is True
    assert providers.PROVIDERS["openai"].files_native is False
    for name in ("deepseek", "kimi", "minimax", "custom_openai_compatible"):
        assert providers.PROVIDERS[name].images_native is False
        assert providers.PROVIDERS[name].files_native is False
