"""Authorization, immutable identity, bounded lifetime and real store accounting."""

import base64
import hashlib
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException

from dlr.control.ai.attachment_documents import (
    DocumentAsset,
    DocumentConversionError,
    DocumentConverterClient,
    DocumentResult,
)
from dlr.control.ai.attachment_store import AiAttachmentStore
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=="
)


class Converter(DocumentConverterClient):
    def __init__(self) -> None:
        super().__init__("http://unused.invalid")
        self.calls = 0
        self.cancelled: list[str] = []
        self.entered = threading.Event()
        self.block = False
        self.fail = False

    def prepare(self, filename, content_type, data, *, job_id, deadline, cancel=None):
        self.calls += 1
        self.entered.set()
        if self.fail:
            raise DocumentConversionError("ai_attachment_parse_failed")
        while self.block:
            if cancel is not None and cancel.wait(0.01):
                raise DocumentConversionError("ai_attachment_cancelled")
            if time.monotonic() >= deadline:
                raise DocumentConversionError("ai_attachment_parse_timeout")
        return DocumentResult(
            "partial_success",
            hashlib.sha256(data).hexdigest(),
            "test-v1",
            3,
            pages=[
                {"page": 1, "status": "ready", "image_asset_id": "first"},
                {"page": 2, "status": "blank"},
                {"page": 3, "status": "available"},
            ],
            sheets=[
                {"name": "经营", "rows": 100, "columns": 5, "hidden": False, "print_area": "A1:E20"}
            ],
            coverage={"pages": "partial", "read_pages": 1},
            warnings=["structure_partial"],
            assets=(self.asset("first"),),
            rendered_pdf=b"%PDF-1.7 derived",
            data={"text": "private business content"},
        )

    def asset(self, identity):
        return DocumentAsset(
            identity, "image/png", PNG, hashlib.sha256(PNG).hexdigest(), len(PNG), 1, 1
        )

    def read_page(
        self,
        filename,
        content_type,
        data,
        *,
        page,
        view="both",
        rendered_pdf=None,
        job_id,
        deadline,
        cancel=None,
    ):
        self.calls += 1
        return DocumentResult(
            "success",
            hashlib.sha256(data).hexdigest(),
            "test-v1",
            3,
            assets=(self.asset("page" + str(page)),),
            data={"text": f"original page {page}"},
            coverage={"page": page, "structure": "partial"},
        )

    def read_sheet(
        self, filename, content_type, data, *, sheet, range_a1, job_id, deadline, cancel=None
    ):
        self.calls += 1
        return DocumentResult(
            "success",
            hashlib.sha256(data).hexdigest(),
            "test-v1",
            3,
            data={"cells": [{"coordinate": "A99", "raw": 99, "formula": None}]},
        )

    def cancel(self, job_id):
        self.cancelled.append(job_id)


def attachment() -> AiAttachment:
    return AiAttachment(
        filename="legal.txt",
        content_type="text/plain",
        data_base64=base64.b64encode(b"original business material").decode(),
    )


@pytest.fixture
def store(tmp_path: Path):
    client = Converter()
    value = AiAttachmentStore(tmp_path / "owned", client, ttl=60)
    yield value, client
    value.close()


def prepared(store, scope=None):
    value, _client = store
    scope = scope or uuid4()
    result = value.prepare(("account", 1), 10, scope, attachment(), deadline=time.monotonic() + 5)
    ref = AiAttachmentReference(
        **{
            key: getattr(result, key)
            for key in ("attachment_ref", "sha256", "representation_revision")
        }
    )
    return scope, result, ref


def test_public_directory_never_contains_binary_paths_or_extracted_content(store) -> None:
    value, _ = store
    scope, result, ref = prepared(store)
    public = result.model_dump_json()
    assert "data_base64" not in public and "private business content" not in public
    assert "instance-" not in public and "rendered_pdf" not in public
    assert result.pages[1] == {"page": 2, "status": "blank"}
    assert result.status == "partial" and result.warnings == ["structure_partial"]
    with value.reader(("account", 1), 10, scope, [ref]) as reader:
        overview = reader.overview()
        assert overview["coverage"]["content_requires_read"] is True
        assert overview["attachments"][0]["attachment_ref"] == result.attachment_ref


def test_original_disk_failure_does_not_leak_unaccounted_directory(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, client = store

    def failed_write(path: Path, data: bytes) -> None:
        path.write_bytes(data[:2])
        raise OSError("simulated disk failure")

    monkeypatch.setattr(value, "_write_bytes", failed_write)
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_unavailable"):
        prepared(store)
    assert value._used_bytes == 0 and not value._records and client.calls == 0
    assert list(value.directory.iterdir()) == [value.directory / ".dlr-ai-owner.json"]


def test_cleanup_failure_keeps_capacity_charge_and_retries_on_sweep(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, _ = store
    scope, result, _ = prepared(store)
    charge = value._used_bytes
    record = value._records[result.attachment_ref]
    original = shutil.rmtree
    attempts = 0

    def transient(path, *args, **kwargs):
        nonlocal attempts
        if path == record.directory:
            attempts += 1
            if attempts == 1:
                raise OSError("transient cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", transient)
    value.release(("account", 1), 10, scope)
    assert record.closed and record.directory.exists()
    assert value._used_bytes == charge and result.attachment_ref in value._records
    with pytest.raises(DocumentConversionError, match="ai_attachment_expired"):
        value._get_locked(record.scope, result.attachment_ref)
    value.sweep()
    assert attempts == 2 and not record.directory.exists()
    assert value._used_bytes == 0 and not value._records


def test_failed_cleanup_after_prepare_cancellation_releases_operation_slots(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, client = store
    client.block = True
    scope = uuid4()
    errors = []

    def start():
        try:
            prepared(store, scope)
        except DocumentConversionError as error:
            errors.append(error.code)

    thread = threading.Thread(target=start)
    thread.start()
    assert client.entered.wait(1)
    record = next(iter(value._records.values()))
    original = shutil.rmtree

    def failed(path, *args, **kwargs):
        if path == record.directory:
            raise OSError("transient cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", failed)
    value.release(("account", 1), 10, scope)
    thread.join(1)
    assert not thread.is_alive() and errors == ["ai_attachment_cancelled"]
    assert record.closed and not record.preparing and value._used_bytes == record.charge
    for semaphore, count in [(value._active, 2), (value._pending, 4)]:
        for _ in range(count):
            assert semaphore.acquire(blocking=False)
        for _ in range(count):
            semaphore.release()
    monkeypatch.setattr(shutil, "rmtree", original)
    client.block = False
    new_scope, fresh, _ = prepared(store)
    assert not record.directory.exists() and len(value._records) == 1
    value.release(("account", 1), 10, new_scope, fresh.attachment_ref)


def test_invalid_converter_pixels_never_publish_ready(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, client = store
    original = client.asset

    def invalid_asset(identity: str) -> DocumentAsset:
        asset = original(identity)
        return DocumentAsset(
            identity, asset.content_type, asset.data, asset.sha256, asset.size_bytes, 4500, 4000
        )

    monkeypatch.setattr(client, "asset", invalid_asset)
    with pytest.raises(DocumentConversionError, match="ai_attachment_converter_invalid_result"):
        prepared(store)
    assert value._used_bytes == 0 and not value._records


@pytest.mark.parametrize(
    "owner,adapter,other_scope",
    [(("account", 2), 10, False), (("account", 1), 11, False), (("account", 1), 10, True)],
)
def test_reference_is_not_authorization(store, owner, adapter, other_scope) -> None:
    value, _ = store
    scope, result, ref = prepared(store)
    scope = uuid4() if other_scope else scope
    with pytest.raises(DocumentConversionError, match="ai_attachment_expired"):
        value.get(owner, adapter, scope, result.attachment_ref)
    value.release(owner, adapter, scope, result.attachment_ref)
    assert len(value._records) == 1
    with (
        pytest.raises(DocumentConversionError, match="ai_attachment_expired"),
        value.reader(owner, adapter, scope, [ref]),
    ):
        pytest.fail("unauthorized pin")


def test_each_read_rechecks_permission_and_frozen_hash_even_on_cache_hit(store) -> None:
    value, client = store
    scope, result, ref = prepared(store)
    authorized = True
    checks = 0

    def check():
        nonlocal checks
        checks += 1
        if not authorized:
            raise HTTPException(403)

    with value.reader(("account", 1), 10, scope, [ref], permission_check=check) as reader:
        page = reader.read_page(result.attachment_ref, 3, "both", timeout_seconds=3)
        assert page.data["page"] == 3 and page.media[0].page == 3
        assert page.media[0].sha256 == hashlib.sha256(PNG).hexdigest()
        assert "data_base64" not in str(page.data)
        calls = client.calls
        reader.read_page(result.attachment_ref, 3, "both", timeout_seconds=3)
        assert client.calls == calls
        authorized = False
        with pytest.raises(HTTPException):
            reader.read_page(result.attachment_ref, 3, "both", timeout_seconds=3)
    assert checks >= 5
    bad = ref.model_copy(update={"sha256": "0" * 64})
    with (
        pytest.raises(DocumentConversionError, match="ai_attachment_hash_mismatch"),
        value.reader(("account", 1), 10, scope, [bad]),
    ):
        pytest.fail("hash mismatch accepted")


def test_original_tamper_and_outside_request_rejected_before_conversion(store) -> None:
    value, client = store
    scope, result, ref = prepared(store)
    calls = client.calls
    with value.reader(("account", 1), 10, scope, [ref]) as reader:
        with pytest.raises(DocumentConversionError, match="ai_attachment_reference_invalid"):
            reader.read_page("outside-reference", 1, "image", timeout_seconds=3)
        record = value._records[result.attachment_ref]
        (record.directory / "original").write_bytes(b"changed")
        with pytest.raises(DocumentConversionError, match="ai_attachment_hash_mismatch"):
            reader.read_page(result.attachment_ref, 1, "image", timeout_seconds=3)
    assert client.calls == calls


def test_active_pin_survives_ttl_and_final_release_frees_every_byte(store) -> None:
    value, _ = store
    scope, result, ref = prepared(store)
    record = value._records[result.attachment_ref]
    directory = record.directory
    with value.reader(("account", 1), 10, scope, [ref]) as reader:
        record.expires = 0
        value.sweep()
        assert directory.exists()
        reader.read_page(result.attachment_ref, 3, "image", timeout_seconds=3)
        value.release(("account", 1), 10, scope)
        assert directory.exists()
        with pytest.raises(DocumentConversionError, match="ai_attachment_expired"):
            reader.overview()
    assert not directory.exists() and value._used_bytes == 0 and not value._records
    with pytest.raises(DocumentConversionError, match="ai_attachment_cancelled"):
        value.prepare(("account", 1), 10, scope, attachment(), deadline=time.monotonic() + 3)


def test_parse_failure_reclaims_original_and_reservation(store) -> None:
    value, client = store
    client.fail = True
    with pytest.raises(DocumentConversionError, match="ai_attachment_parse_failed"):
        value.prepare(("account", 1), 10, uuid4(), attachment(), deadline=time.monotonic() + 3)
    assert value._used_bytes == 0 and not value._records
    assert list(value.directory.iterdir()) == [value.directory / ".dlr-ai-owner.json"]


def test_scope_cancel_while_preparing_fences_publication_and_recovers(store) -> None:
    value, client = store
    client.block = True
    scope = uuid4()
    errors = []

    def prepare():
        try:
            value.prepare(("account", 1), 10, scope, attachment(), deadline=time.monotonic() + 3)
        except DocumentConversionError as error:
            errors.append(error.code)

    worker = threading.Thread(target=prepare)
    worker.start()
    assert client.entered.wait(1)
    value.release(("account", 1), 10, scope)
    worker.join(1)
    assert not worker.is_alive() and errors == ["ai_attachment_cancelled"]
    assert client.cancelled and not value._records and value._used_bytes == 0
    client.block = False
    prepared(store)


def test_capacity_accounting_includes_metadata_cache_and_derivatives(store) -> None:
    value, _ = store
    scope, result, ref = prepared(store)
    record = value._records[result.attachment_ref]
    actual_files = sum(file.stat().st_size for file in record.directory.iterdir())
    assert value._used_bytes > actual_files  # metadata is charged too
    with value.reader(("account", 1), 10, scope, [ref]) as reader:
        before = value._used_bytes
        reader.read_sheet(result.attachment_ref, "经营", "A99:B99", timeout_seconds=3)
        assert value._used_bytes > before
        value.max_bytes = value._used_bytes
        with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
            value.prepare(("account", 1), 10, uuid4(), attachment(), deadline=time.monotonic() + 3)
    value.release(("account", 1), 10, scope)
    assert value._used_bytes == 0


def test_single_control_owner_and_restart_only_removes_marked_orphans(tmp_path) -> None:
    root = tmp_path / "owned"
    first = AiAttachmentStore(root, Converter())
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_busy"):
        AiAttachmentStore(root, Converter())
    first.close()
    orphan = root / "instance-orphan"
    orphan.mkdir()
    (orphan / ".dlr-ai-owner.json").write_text('{"schema":"dlr-ai-ephemeral-v1"}')
    (orphan / "raw").write_bytes(b"task-owned")
    unrelated = root / "instance-user-material"
    unrelated.mkdir()
    (unrelated / "retained").write_bytes(b"user")
    second = AiAttachmentStore(root, Converter())
    assert not orphan.exists() and (unrelated / "retained").read_bytes() == b"user"
    second.close()
