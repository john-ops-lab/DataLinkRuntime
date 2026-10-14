"""Private converter wire bytes and actual cancellation acknowledgement contract."""

import base64
import hashlib
import threading
import time
from typing import Any

import pytest

from dlr.control.ai.attachment_documents import (
    DocumentConversionError,
    DocumentConverterClient,
    _result,
)


def test_result_separates_private_binary_from_manifest() -> None:
    data = b"original"
    asset = b"png pixels"
    payload = {
        "status": "partial_success",
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "representation_revision": "v1",
        "page_count": 3,
        "pages": [{"page": 3, "status": "ready"}],
        "coverage": {"structure": "partial"},
        "warnings": ["structure_partial"],
        "assets": [
            {
                "asset_id": "abc123",
                "content_type": "image/png",
                "data_base64": base64.b64encode(asset).decode(),
                "sha256": hashlib.sha256(asset).hexdigest(),
                "size_bytes": len(asset),
                "width": 10,
                "height": 20,
            }
        ],
        "rendered_pdf": base64.b64encode(b"PDF").decode(),
        "data": {"text": "page3"},
    }
    result = _result(payload, data)
    assert result.assets[0].data == asset
    assert result.rendered_pdf == b"PDF"
    assert result.data == {"text": "page3"}
    assert "data_base64" not in str(result.pages)
    payload["assets"][0]["sha256"] = "0" * 64
    with pytest.raises(DocumentConversionError, match="invalid_result"):
        _result(payload, data)


@pytest.mark.parametrize("reason", ["cancel", "deadline"])
def test_client_waits_for_remote_reaping(reason: str) -> None:
    stop = threading.Event()
    cancelled = threading.Event()
    ack = []

    class Converter(DocumentConverterClient):
        def _http(
            self, path: str, payload: dict[str, Any] | None, timeout: float, *, method: str = "POST"
        ) -> dict[str, Any]:
            if method == "DELETE":
                ack.append(path)
                stop.set()
                return {"terminated": True}
            assert stop.wait(2)
            raise DocumentConversionError("ai_attachment_cancelled")

    client = Converter("http://converter")
    if reason == "cancel":
        timer = threading.Timer(0.05, cancelled.set)
        timer.start()
    with pytest.raises(DocumentConversionError, match="cancelled|timeout"):
        client.prepare(
            "sample.pdf",
            "application/pdf",
            b"pdf",
            job_id="job123",
            deadline=time.monotonic() + (0.05 if reason == "deadline" else 2),
            cancel=cancelled,
        )
    assert ack == ["/v1/jobs/job123"]
    assert stop.is_set()


def test_cancel_requires_termination_confirmation() -> None:
    class Converter(DocumentConverterClient):
        def _http(
            self, path: str, payload: dict[str, Any] | None, timeout: float, *, method: str = "POST"
        ) -> dict[str, Any]:
            return {"terminated": False}

    with pytest.raises(DocumentConversionError, match="cancel_failed"):
        Converter("http://converter").cancel("job123")


def test_single_page_pdf_asset_requires_magic_and_one_page() -> None:
    source = b"source"
    pdf = b"%PDF-1.7\nencoded server page"
    payload = {
        "status": "success",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "representation_revision": "v1",
        "page_count": 12,
        "assets": [
            {
                "asset_id": "pdfasset",
                "content_type": "application/pdf",
                "data_base64": base64.b64encode(pdf).decode(),
                "sha256": hashlib.sha256(pdf).hexdigest(),
                "size_bytes": len(pdf),
                "page_count": 1,
            }
        ],
    }
    assert _result(payload, source).assets[0].page_count == 1
    payload["assets"][0]["page_count"] = 2
    with pytest.raises(DocumentConversionError, match="invalid_result"):
        _result(payload, source)


def test_http_rejects_redirect_and_never_echoes_arbitrary_error_code() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests: list[str] = []

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            self.send_response(307)
            self.send_header("Location", "/leak")
            self.end_headers()
            self.wfile.write(b'{"code":"secret-file-name-or-body"}')

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        client = DocumentConverterClient(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(DocumentConversionError, match="ai_attachment_converter_unavailable"):
            client._http("/prepare", {"data_base64": "private"}, 2)
        assert requests == ["/prepare"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    "pixels,size,error",
    [(16_000_001, 1, "image_too_large"), (1, 10 * 1024 * 1024 + 1, "too_large")],
)
def test_private_client_rejects_media_outside_reader_bounds(pixels, size, error) -> None:
    source = b"source"
    asset = b"x" * size
    payload = {
        "status": "success",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "assets": [
            {
                "asset_id": "bounded",
                "content_type": "image/png",
                "data_base64": base64.b64encode(asset).decode(),
                "sha256": hashlib.sha256(asset).hexdigest(),
                "size_bytes": size,
                "width": pixels,
                "height": 1,
            }
        ],
    }
    with pytest.raises(DocumentConversionError, match=error):
        _result(payload, source)


@pytest.mark.parametrize("body", [b"[]", b"null", b'"private-error-text"', b"123"])
def test_http_error_non_object_json_stays_stable(monkeypatch, body: bytes) -> None:
    import io
    import urllib.error
    import urllib.request

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> None:
            raise urllib.error.HTTPError("http://converter", 503, "failed", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(DocumentConversionError) as error:
        DocumentConverterClient("http://converter")._http("/v1/prepare", {}, 1)
    assert error.value.code == "ai_attachment_converter_unavailable"
    assert "private-error-text" not in str(error.value)
