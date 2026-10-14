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
