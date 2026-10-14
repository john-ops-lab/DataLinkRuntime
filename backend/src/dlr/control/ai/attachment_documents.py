"""Private conversion wire contract. Control has no Provider or Docker operations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing, suppress
from dataclasses import dataclass, field
from typing import Any

MAX_ATTACHMENT_MEDIA_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_PIXELS = 16_000_000
MAX_DERIVED_BYTES = 64 * 1024 * 1024
MAX_WIRE_BYTES = 96 * 1024 * 1024


class DocumentConversionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DocumentAsset:
    asset_id: str
    content_type: str
    data: bytes
    sha256: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    page_count: int | None = None


@dataclass(frozen=True)
class DocumentResult:
    status: str
    source_sha256: str
    representation_revision: str
    page_count: int
    pages: list[dict[str, Any]] = field(default_factory=list)
    sheets: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    assets: tuple[DocumentAsset, ...] = ()
    rendered_pdf: bytes | None = None
    data: dict[str, Any] = field(default_factory=dict)


def _decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise DocumentConversionError("ai_attachment_converter_invalid_result")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DocumentConversionError("ai_attachment_converter_invalid_result") from exc
    if len(result) > MAX_DERIVED_BYTES:
        raise DocumentConversionError("ai_attachment_too_large")
    return result


def _result(payload: dict[str, Any], source: bytes) -> DocumentResult:
    digest = hashlib.sha256(source).hexdigest()
    if payload.get("source_sha256") != digest:
        raise DocumentConversionError("ai_attachment_converter_invalid_result")
    assets = []
    for row in payload.get("assets", []):
        data = _decode(row.get("data_base64"))
        if not 0 < len(data) <= MAX_ATTACHMENT_MEDIA_BYTES:
            raise DocumentConversionError("ai_attachment_too_large")
        asset_digest = hashlib.sha256(data).hexdigest()
        asset_id = row.get("asset_id")
        if (
            row.get("sha256") != asset_digest
            or row.get("size_bytes") != len(data)
            or not isinstance(asset_id, str)
            or not asset_id.isalnum()
            or len(asset_id) > 80
            or row.get("content_type")
            not in {"image/png", "image/jpeg", "image/webp", "application/pdf"}
        ):
            raise DocumentConversionError("ai_attachment_converter_invalid_result")
        if str(row.get("content_type", "")).startswith("image/") and (
            type(row.get("width")) is not int
            or type(row.get("height")) is not int
            or row["width"] <= 0
            or row["height"] <= 0
            or row["width"] * row["height"] > MAX_ATTACHMENT_PIXELS
        ):
            raise DocumentConversionError("ai_attachment_image_too_large")
        if row.get("content_type") == "application/pdf" and (
            not data.startswith(b"%PDF-")
            or type(row.get("page_count")) is not int
            or row["page_count"] != 1
        ):
            raise DocumentConversionError("ai_attachment_converter_invalid_result")
        assets.append(
            DocumentAsset(
                asset_id,
                row["content_type"],
                data,
                asset_digest,
                len(data),
                row.get("width"),
                row.get("height"),
                row.get("page_count"),
            )
        )
    pdf = _decode(payload["rendered_pdf"]) if payload.get("rendered_pdf") else None
    if sum(len(x.data) for x in assets) + len(pdf or b"") > MAX_DERIVED_BYTES:
        raise DocumentConversionError("ai_attachment_too_large")
    if payload.get("status") not in {"success", "partial_success", "skipped", "failure"}:
        raise DocumentConversionError("ai_attachment_converter_invalid_result")
    return DocumentResult(
        status=payload["status"],
        source_sha256=digest,
        representation_revision=str(payload.get("representation_revision", "")),
        page_count=int(payload.get("page_count", 0)),
        pages=payload.get("pages", []),
        sheets=payload.get("sheets", []),
        coverage=payload.get("coverage", {}),
        warnings=payload.get("warnings", []),
        assets=tuple(assets),
        rendered_pdf=pdf,
        data=payload.get("data", {}),
    )


KNOWN_ERROR_CODES = frozenset(
    {
        "ai_attachment_invalid",
        "ai_attachment_filename_invalid",
        "ai_attachment_type_unsupported",
        "ai_attachment_too_large",
        "ai_attachment_converter_invalid_result",
        "ai_attachment_converter_unavailable",
        "ai_attachment_converter_busy",
        "ai_attachment_cancel_failed",
        "ai_attachment_cancelled",
        "ai_attachment_parse_timeout",
        "ai_attachment_parse_failed",
        "ai_attachment_no_text",
        "ai_attachment_unsafe_archive",
        "ai_attachment_range_exceeded",
        "ai_attachment_sheet_not_found",
        "ai_attachment_page_not_found",
        "ai_attachment_page_limit",
        "ai_attachment_image_too_large",
        "ai_attachment_encrypted",
    }
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class DocumentConverterClient:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def _http(
        self, path: str, payload: dict[str, Any] | None, timeout: float, *, method: str = "POST"
    ) -> dict[str, Any]:
        encoded = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.url + path,
            data=encoded,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_WIRE_BYTES + 1)
            if len(body) > MAX_WIRE_BYTES:
                raise DocumentConversionError("ai_attachment_too_large")
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("invalid response")
            return parsed
        except urllib.error.HTTPError as exc:
            code = "ai_attachment_converter_unavailable"
            with suppress(OSError, ValueError, TypeError), closing(exc):
                body = json.loads(exc.read(4096))
                candidate = body.get("code") if isinstance(body, dict) else None
                if candidate in KNOWN_ERROR_CODES:
                    code = candidate
            raise DocumentConversionError(code) from exc
        except (OSError, ValueError) as exc:
            raise DocumentConversionError("ai_attachment_converter_unavailable") from exc

    def cancel(self, job_id: str) -> None:
        if not job_id or not all(c.isalnum() or c in "_-" for c in job_id):
            raise DocumentConversionError("ai_attachment_invalid")
        reply = self._http(f"/v1/jobs/{job_id}", None, 5.0, method="DELETE")
        if reply.get("terminated") is not True:
            raise DocumentConversionError("ai_attachment_cancel_failed")

    def _call(
        self,
        path: str,
        filename: str,
        content_type: str,
        data: bytes,
        *,
        job_id: str,
        deadline: float,
        cancel: threading.Event | None,
        **options: Any,
    ) -> DocumentResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (cancel is not None and cancel.is_set()):
            raise DocumentConversionError(
                "ai_attachment_parse_timeout" if remaining <= 0 else "ai_attachment_cancelled"
            )
        payload = {
            "job_id": job_id,
            "filename": filename,
            "content_type": content_type,
            "data_base64": base64.b64encode(data).decode(),
            "timeout_seconds": min(60.0, remaining),
            **options,
        }
        replies: list[dict[str, Any]] = []
        errors: list[Exception] = []

        def transport() -> None:
            try:
                replies.append(self._http(path, payload, remaining + 2))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=transport, daemon=True)
        worker.start()
        while worker.is_alive():
            if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                self.cancel(job_id)
                worker.join(timeout=2)
                raise DocumentConversionError(
                    "ai_attachment_cancelled"
                    if cancel is not None and cancel.is_set()
                    else "ai_attachment_parse_timeout"
                )
            worker.join(timeout=0.05)
        if errors:
            self.cancel(job_id)
            raise errors[0]
        return _result(replies[0], data)

    def prepare(
        self,
        filename: str,
        content_type: str,
        data: bytes,
        *,
        job_id: str,
        deadline: float,
        cancel: threading.Event | None = None,
    ) -> DocumentResult:
        return self._call(
            "/v1/prepare",
            filename,
            content_type,
            data,
            job_id=job_id,
            deadline=deadline,
            cancel=cancel,
        )

    def read_page(
        self,
        filename: str,
        content_type: str,
        data: bytes,
        *,
        page: int,
        view: str = "both",
        rendered_pdf: bytes | None = None,
        job_id: str,
        deadline: float,
        cancel: threading.Event | None = None,
    ) -> DocumentResult:
        options: dict[str, Any] = {"page": page, "view": view}
        if rendered_pdf is not None:
            options["rendered_pdf_base64"] = base64.b64encode(rendered_pdf).decode()
        return self._call(
            "/v1/read-page",
            filename,
            content_type,
            data,
            job_id=job_id,
            deadline=deadline,
            cancel=cancel,
            **options,
        )

    def read_sheet(
        self,
        filename: str,
        content_type: str,
        data: bytes,
        *,
        sheet: str,
        range_a1: str,
        job_id: str,
        deadline: float,
        cancel: threading.Event | None = None,
    ) -> DocumentResult:
        return self._call(
            "/v1/read-sheet",
            filename,
            content_type,
            data,
            job_id=job_id,
            deadline=deadline,
            cancel=cancel,
            sheet=sheet,
            range_a1=range_a1,
        )
