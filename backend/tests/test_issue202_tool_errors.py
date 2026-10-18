"""Production scoped readers retain safe attachment failures through the dispatcher."""

import base64
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from dlr.control.ai import tools
from dlr.control.ai.attachment_documents import (
    DocumentConversionError,
    DocumentConverterClient,
    DocumentResult,
)
from dlr.control.ai.attachment_reading import ScopedAttachmentReader
from dlr.control.ai.attachment_store import AiAttachmentStore
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference

OWNER = ("account", 1)
ADAPTER_ID = 10
PRIVATE_DETAIL = "private-operation-detail-/internal/files/customer.txt"


def conversion_error(code: str) -> DocumentConversionError:
    error = DocumentConversionError(code)
    error.args = (code, PRIVATE_DETAIL)
    return error


class Converter(DocumentConverterClient):
    """Only the conversion transport is synthetic; store and reader are production code."""

    def __init__(self) -> None:
        super().__init__("http://unused.invalid")
        self.failure: Exception | None = None
        self.reads: list[str] = []

    def prepare(self, filename, content_type, data, **kwargs):
        return DocumentResult(
            "success",
            hashlib.sha256(data).hexdigest(),
            "test-v1",
            1,
            pages=[{"page": 1, "status": "ready"}],
            sheets=[{"name": "Data", "rows": 1, "columns": 1}],
            coverage={"pages": "success", "read_pages": 1},
        )

    def _read(self, data: bytes, kind: str) -> DocumentResult:
        self.reads.append(kind)
        if self.failure is not None:
            raise self.failure
        return DocumentResult(
            "success",
            hashlib.sha256(data).hexdigest(),
            "test-v1",
            1,
            data=(
                {"text": "source page"}
                if kind == "page"
                else {"cells": [{"coordinate": "A1", "raw_value": 17}]}
            ),
            coverage={"pages": "success", "read_pages": 1},
        )

    def read_page(self, filename, content_type, data, **kwargs):
        return self._read(data, "page")

    def read_sheet(self, filename, content_type, data, **kwargs):
        return self._read(data, "sheet")

    def cancel(self, job_id):
        pass


@pytest.fixture
def prepared(
    tmp_path: Path,
) -> Iterator[tuple[AiAttachmentStore, Converter, UUID, AiAttachmentReference]]:
    client = Converter()
    store = AiAttachmentStore(tmp_path / "owned", client)
    scope = uuid4()
    metadata = store.prepare(
        OWNER,
        ADAPTER_ID,
        scope,
        AiAttachment(
            filename="source.txt",
            content_type="text/plain",
            data_base64=base64.b64encode(b"source material").decode(),
        ),
        deadline=time.monotonic() + 5,
    )
    ref = AiAttachmentReference.model_validate(
        metadata.model_dump(include={"attachment_ref", "sha256", "representation_revision"})
    )
    yield store, client, scope, ref
    store.close()


def execute(
    reader: ScopedAttachmentReader, ref: AiAttachmentReference, operation: str, **args: object
) -> tools.ToolExecution:
    arguments = (
        {}
        if operation == "list"
        else {"attachment_ref": ref.attachment_ref, "page": 1, "view": "text"}
        if operation == "page"
        else {"attachment_ref": ref.attachment_ref, "sheet": "Data", "range": "A1"}
    )
    return tools.execute_tool_call(
        {
            "list": "list_attachments",
            "page": "read_attachment_page",
            "sheet": "read_attachment_sheet",
        }[operation],
        json.dumps({**arguments, **args}),
        None,
        context=tools.ToolExecutionContext(
            attachment_reader=reader,
            attachment_refs=frozenset({ref.attachment_ref}),
            hard_deadline=time.monotonic() + 5,
        ),
    )


@pytest.mark.parametrize("operation", ["list", "page", "sheet"])
def test_closed_reference_preserves_expired_code(prepared, operation: str) -> None:
    store, client, scope, ref = prepared
    with store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader:
        store.release(OWNER, ADAPTER_ID, scope)
        result = execute(reader, ref, operation)
    assert result.status == "error"
    assert result.error_code == "ai_attachment_expired"
    assert not client.reads and not store._records and store._used_bytes == 0


@pytest.mark.parametrize("operation", ["page", "sheet"])
def test_real_reader_original_digest_failure_is_not_a_generic_error(
    prepared, operation: str
) -> None:
    store, client, scope, ref = prepared
    with store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader:
        (store._records[ref.attachment_ref].directory / "original").write_bytes(b"tampered")
        result = execute(reader, ref, operation)
    assert result.error_code == "ai_attachment_hash_mismatch"
    assert not client.reads


@pytest.mark.parametrize("operation,args", [("page", {"page": 2}), ("sheet", {"sheet": "Other"})])
def test_production_reader_range_failure_preserves_semantics(prepared, operation, args) -> None:
    store, client, scope, ref = prepared
    with store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader:
        result = execute(reader, ref, operation, **args)
    assert result.error_code == "ai_attachment_range_invalid"
    assert not client.reads


@pytest.mark.parametrize("operation", ["page", "sheet"])
@pytest.mark.parametrize(
    "code,expected",
    [
        ("ai_attachment_cancelled", "ai_attachment_cancelled"),
        ("ai_attachment_parse_timeout", "ai_tool_timeout"),
        ("ai_attachment_too_large", "ai_attachment_too_large"),
        ("ai_attachment_converter_busy", "ai_attachment_converter_busy"),
        ("ai_attachment_parse_failed", "ai_attachment_parse_failed"),
    ],
)
def test_converter_failures_cross_real_reader_and_dispatcher_without_details(
    prepared, caplog: pytest.LogCaptureFixture, operation: str, code: str, expected: str
) -> None:
    store, client, scope, ref = prepared
    client.failure = conversion_error(code)
    with (
        caplog.at_level(logging.INFO, logger="dlr.ai.tools"),
        store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader,
    ):
        result = execute(reader, ref, operation)
        client.failure = None
        recovered = execute(reader, ref, operation)
    assert result.status == "error" and result.error_code == expected
    assert PRIVATE_DETAIL not in json.dumps(asdict(result)) + caplog.text
    assert json.loads(result.model_content) == {
        "ok": False,
        "error_code": expected,
        "error": "tool call rejected or failed",
    }
    assert recovered.status == "success" and len(client.reads) == 2


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError(PRIVATE_DETAIL),
        conversion_error("ai_attachment_unrecognized_detail"),
        DocumentConversionError(PRIVATE_DETAIL),
    ],
)
def test_unknown_reader_failures_stay_generic_and_do_not_reflect_exception(
    prepared, caplog: pytest.LogCaptureFixture, failure: Exception
) -> None:
    store, client, scope, ref = prepared
    client.failure = failure
    with (
        caplog.at_level(logging.INFO, logger="dlr.ai.tools"),
        store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader,
    ):
        result = execute(reader, ref, "page")
    assert result.error_code == "ai_tool_failed"
    assert str(failure) not in json.dumps(asdict(result)) + caplog.text
    assert PRIVATE_DETAIL not in json.dumps(asdict(result)) + caplog.text


@pytest.mark.parametrize(
    "detail,expected",
    [
        ({"code": "ai_attachment_expired", "message": PRIVATE_DETAIL}, "ai_attachment_expired"),
        ({"code": "ai_attachment_parse_timeout"}, "ai_attachment_parse_timeout"),
        ({"code": "ai_attachment_custom_failure"}, "ai_attachment_custom_failure"),
        ({"code": PRIVATE_DETAIL}, "ai_tool_failed"),
        (PRIVATE_DETAIL, "ai_tool_failed"),
    ],
)
def test_existing_http_reader_exception_compatibility(prepared, detail, expected) -> None:
    store, client, scope, ref = prepared
    client.failure = HTTPException(status_code=410, detail=detail)
    with store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader:
        result = execute(reader, ref, "page")
    assert result.error_code == expected
    assert PRIVATE_DETAIL not in json.dumps(asdict(result))


@pytest.mark.parametrize("operation", ["list", "page", "sheet"])
def test_successful_production_reader_dispatch_keeps_source_identity(
    prepared, operation: str
) -> None:
    store, client, scope, ref = prepared
    with store.reader(OWNER, ADAPTER_ID, scope, [ref]) as reader:
        result = execute(reader, ref, operation)
    assert result.status == "success" and result.error_code is None
    content = json.loads(result.model_content)
    if operation == "list":
        assert content["attachments"][0]["sha256"] == ref.sha256
        assert not client.reads
    else:
        assert content["attachment_ref"] == ref.attachment_ref
        assert content["source_sha256"] == ref.sha256
        assert content["representation_revision"] == ref.representation_revision
        assert client.reads == [operation]
