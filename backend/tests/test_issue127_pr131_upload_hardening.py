"""PR #131 review regressions for upload finalization and multipart budgets."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from dlr.common.config import settings
from dlr.control.api import managed_input as managed_input_api
from dlr.control.models import (
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputReservationStatus,
    ManagedInputUploadReservation,
)
from dlr.control.security import Principal
from dlr.control.services import managed_input_upload
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.managed_input_upload import UploadSessionState
from dlr.control.services.multipart import MultipartParseError, MultipartReader


def _create_task(api_client: Any, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _set_max_file_bytes(session_factory: sessionmaker[Session], max_file_bytes: int) -> None:
    from dlr.control.models import ManagedInputSettings

    with session_factory() as session:
        policy = session.get(ManagedInputSettings, 1)
        assert policy is not None
        policy.max_file_bytes = max_file_bytes
        session.commit()


def _request(
    adapter_id: int,
    boundary: str,
    chunks: list[bytes],
    *,
    on_chunk: Any | None = None,
) -> Request:
    indexed_chunks = iter(enumerate(chunks))
    remaining = len(chunks)

    async def receive() -> dict[str, object]:
        nonlocal remaining
        try:
            index, body = next(indexed_chunks)
        except StopIteration:
            return {"type": "http.disconnect"}
        if on_chunk is not None:
            on_chunk(index)
        remaining -= 1
        return {
            "type": "http.request",
            "body": body,
            "more_body": remaining > 0,
        }

    path = f"/api/adapters/{adapter_id}/input-artifacts"
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 1),
            "root_path": "",
        },
        receive,
    )


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _single_file_body(boundary: str, payload: bytes = b"payload") -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="review.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )


def test_multipart_rejects_header_over_limit_even_when_separator_is_buffered() -> None:
    boundary = "review-header-limit"
    base = b'Content-Disposition: form-data; name="file"; filename="review.txt"\r\nX: '

    def body_with_header_size(size: int) -> bytes:
        raw_headers = base + b"x" * (size - len(base))
        return (
            f"--{boundary}\r\n".encode()
            + raw_headers
            + b"\r\n\r\npayload"
            + f"\r\n--{boundary}--\r\n".encode()
        )

    async def run() -> None:
        prefix = f"--{boundary}\r\n".encode()
        for separator_prefix_bytes in range(1, 4):
            exact_body = body_with_header_size(MultipartReader.HEADER_LIMIT)
            exact_separator = exact_body.index(b"\r\n\r\n", len(prefix))
            exact_split = exact_separator + separator_prefix_bytes
            exact_reader = MultipartReader(
                _chunks(exact_body[:exact_split], exact_body[exact_split:]),
                f"multipart/form-data; boundary={boundary}",
                max_total_bytes=len(exact_body),
            )
            assert await exact_reader.next_part() is not None
            assert b"".join([chunk async for chunk in exact_reader.iter_part_body()]) == b"payload"
            await exact_reader.ensure_complete()

            oversized_body = body_with_header_size(MultipartReader.HEADER_LIMIT + 1)
            oversized_separator = oversized_body.index(b"\r\n\r\n", len(prefix))
            oversized_split = oversized_separator + separator_prefix_bytes
            oversized_reader = MultipartReader(
                _chunks(oversized_body[:oversized_split], oversized_body[oversized_split:]),
                f"multipart/form-data; boundary={boundary}",
                max_total_bytes=len(oversized_body),
            )
            with pytest.raises(MultipartParseError, match="headers are too large"):
                await oversized_reader.next_part()

    asyncio.run(run())


def test_multipart_total_budget_accepts_exact_size_and_rejects_one_byte_over() -> None:
    boundary = "review-total-budget"
    body = _single_file_body(boundary)

    async def consume(max_total_bytes: int) -> bytes:
        reader = MultipartReader(
            _chunks(body),
            f"multipart/form-data; boundary={boundary}",
            max_total_bytes=max_total_bytes,
        )
        part = await reader.next_part()
        assert part is not None and part.filename == "review.txt"
        payload = b"".join([chunk async for chunk in reader.iter_part_body()])
        assert await reader.next_part() is None
        await reader.ensure_complete()
        return payload

    assert asyncio.run(consume(len(body))) == b"payload"
    with pytest.raises(MultipartParseError, match="body is too large"):
        asyncio.run(consume(len(body) - 1))


def test_multipart_epilogue_has_an_independent_small_budget() -> None:
    boundary = "review-epilogue-budget"
    body = _single_file_body(boundary)
    exact_epilogue = b" " * MultipartReader.EPILOGUE_LIMIT

    async def consume(epilogue: bytes) -> MultipartReader:
        reader = MultipartReader(
            _chunks(body, epilogue[:4096], epilogue[4096:]),
            f"multipart/form-data; boundary={boundary}",
            max_total_bytes=len(body) + len(epilogue),
        )
        assert await reader.next_part() is not None
        assert b"".join([chunk async for chunk in reader.iter_part_body()]) == b"payload"
        await reader.ensure_complete()
        return reader

    asyncio.run(consume(exact_epilogue))

    async def reject() -> None:
        reader = MultipartReader(
            _chunks(body, exact_epilogue, b" "),
            f"multipart/form-data; boundary={boundary}",
            max_total_bytes=len(body) + len(exact_epilogue) + 1,
        )
        assert await reader.next_part() is not None
        async for _ in reader.iter_part_body():
            pass
        with pytest.raises(MultipartParseError, match="epilogue is too large"):
            await reader.ensure_complete()
        assert not reader._buffer

    asyncio.run(reject())


def test_multipart_missing_final_boundary_stops_at_total_byte_budget() -> None:
    boundary = "review-missing-boundary"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="review.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    pulls = 0

    async def endless_body() -> AsyncIterator[bytes]:
        nonlocal pulls
        pulls += 1
        yield prefix
        while True:
            pulls += 1
            yield b"x" * 16

    async def run() -> None:
        reader = MultipartReader(
            endless_body(),
            f"multipart/form-data; boundary={boundary}",
            max_total_bytes=len(prefix) + 64,
        )
        assert await reader.next_part() is not None
        with pytest.raises(MultipartParseError, match="body is too large"):
            async for _ in reader.iter_part_body():
                pass

    asyncio.run(run())
    assert pulls <= 6


def test_oversize_upload_error_is_stable_across_asgi_chunking(
    api_client: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    max_file_bytes = 1024 * 1024
    _set_max_file_bytes(session_factory, max_file_bytes)
    boundary = "review-oversize-chunking"
    body = _single_file_body(
        boundary,
        b"x" * (max_file_bytes + MultipartReader.REQUEST_OVERHEAD_LIMIT + 1),
    )
    chunk_variants = [
        [body],
        [body[offset : offset + 64 * 1024] for offset in range(0, len(body), 64 * 1024)],
    ]

    for index, chunks in enumerate(chunk_variants):
        adapter = _create_task(api_client, f"review-oversize-chunking-{index}")
        with pytest.raises(HTTPException) as caught:
            asyncio.run(
                managed_input_api._stream_upload(
                    _request(adapter["id"], boundary, chunks),
                    adapter["id"],
                    Principal(kind="superadmin"),
                )
            )
        assert caught.value.status_code == 413
        assert caught.value.detail == {
            "code": "input_file_too_large",
            "message": "Input file is too large",
        }

    with session_factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == capacity.reserved_bytes == 0
        live = session.scalars(
            select(ManagedInputArtifact).where(
                ManagedInputArtifact.status.in_(["UPLOADING", "STAGED"])
            )
        ).all()
        assert live == []


@pytest.mark.parametrize("field_first", [True, False])
def test_upload_rejects_large_ordinary_field_with_bounded_cleanup(
    api_client: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field_first: bool,
) -> None:
    adapter = _create_task(api_client, f"review-large-field-{field_first}")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    _set_max_file_bytes(session_factory, 1024 * 1024)
    boundary = "review-field-budget"
    field_header = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
    ).encode()
    file_part_header_and_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="review.txt"\r\n'
        "Content-Type: text/plain\r\n\r\npayload"
    ).encode()
    if field_first:
        first_chunk = field_header
        tail = (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="review.txt"\r\n'
            "Content-Type: text/plain\r\n\r\npayload"
            f"\r\n--{boundary}--\r\n"
        ).encode()
    else:
        first_chunk = (
            file_part_header_and_body
            + f"\r\n--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="metadata"\r\n\r\n'
        )
        tail = f"\r\n--{boundary}--\r\n".encode()
    field_chunks = [b"x" * (16 * 1024) for _ in range(6)]
    request_chunks = [first_chunk, *field_chunks, tail]
    tail_pulled = False

    def on_chunk(index: int) -> None:
        nonlocal tail_pulled
        if index == len(request_chunks) - 1:
            tail_pulled = True

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            managed_input_api._stream_upload(
                _request(
                    adapter["id"],
                    boundary,
                    request_chunks,
                    on_chunk=on_chunk,
                ),
                adapter["id"],
                Principal(kind="superadmin"),
            )
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "input_invalid"
    assert tail_pulled is False
    with session_factory() as session:
        live = session.scalars(
            select(ManagedInputArtifact).where(
                ManagedInputArtifact.adapter_id == adapter["id"],
                ManagedInputArtifact.status.in_(["UPLOADING", "STAGED"]),
            )
        ).all()
        assert live == []
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == capacity.reserved_bytes == 0
    assert not [path for path in store_root.rglob("*") if path.is_file()]


def test_upload_rejects_second_file_without_pulling_its_body(
    api_client: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _create_task(api_client, "review-second-file")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    boundary = "review-second-file"
    first_and_second_header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="first.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nfirst"
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="second.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    second_body = f"second\r\n--{boundary}--\r\n".encode()
    second_body_pulled = False

    def on_chunk(index: int) -> None:
        nonlocal second_body_pulled
        if index == 1:
            second_body_pulled = True

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            managed_input_api._stream_upload(
                _request(
                    adapter["id"],
                    boundary,
                    [first_and_second_header, second_body],
                    on_chunk=on_chunk,
                ),
                adapter["id"],
                Principal(kind="superadmin"),
            )
        )
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "input_invalid"
    assert second_body_pulled is False
    with session_factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == capacity.reserved_bytes == 0
    assert not [path for path in store_root.rglob("*") if path.is_file()]


def test_cancel_after_finalize_commit_preserves_staged_blob(
    api_client: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _create_task(api_client, "review-finalize-cancel")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    boundary = "review-finalize-cancel"
    payload = b"finalized payload must survive cancellation"
    request = _request(adapter["id"], boundary, [_single_file_body(boundary, payload)])
    original_consume = managed_input_upload.consume_upload_reservation
    original_abort = managed_input_upload.abort_upload
    committed = threading.Event()
    release_worker = threading.Event()
    abort_results: list[Any] = []

    def consume_then_block(*args: Any, **kwargs: Any) -> ManagedInputArtifact:
        artifact = original_consume(*args, **kwargs)
        committed.set()
        release_worker.wait(timeout=10)
        return artifact

    def capture_abort(*args: Any, **kwargs: Any) -> Any:
        result = original_abort(*args, **kwargs)
        abort_results.append(result)
        return result

    monkeypatch.setattr(
        managed_input_upload,
        "consume_upload_reservation",
        consume_then_block,
    )
    monkeypatch.setattr(managed_input_upload, "abort_upload", capture_abort)

    async def race() -> None:
        task = asyncio.create_task(
            managed_input_api._stream_upload(
                request,
                adapter["id"],
                Principal(kind="superadmin"),
            )
        )
        try:
            assert await asyncio.to_thread(committed.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            release_worker.set()

    asyncio.run(race())
    assert len(abort_results) == 1
    assert abort_results[0].outcome == "finalized"
    assert abort_results[0].cleanup_authorized is False

    store = LocalFileArtifactStore(store_root)
    with session_factory() as session:
        artifact = session.scalar(
            select(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == adapter["id"])
        )
        assert artifact is not None
        assert artifact.status == "STAGED"
        assert artifact.size_bytes == len(payload)
        assert store.stat(artifact.storage_key) is not None
        reservation = session.get(ManagedInputUploadReservation, artifact.upload_reservation_id)
        assert reservation is not None
        assert reservation.status == ManagedInputReservationStatus.CONSUMED
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == len(payload)
        assert capacity.reserved_bytes == 0


def test_cancel_before_finalize_lock_wins_and_late_finalize_cannot_publish(
    api_client: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _create_task(api_client, "review-cancel-first")
    store_root = tmp_path / "store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    boundary = "review-cancel-first"
    request = _request(
        adapter["id"],
        boundary,
        [_single_file_body(boundary, b"cancelled payload")],
    )
    original_consume = managed_input_upload.consume_upload_reservation
    original_abort = managed_input_upload.abort_upload
    consume_entered = threading.Event()
    release_consume = threading.Event()
    consume_finished = threading.Event()
    abort_finished = threading.Event()
    consume_errors: list[BaseException] = []
    abort_results: list[Any] = []

    def consume_after_abort(*args: Any, **kwargs: Any) -> ManagedInputArtifact:
        consume_entered.set()
        try:
            if not release_consume.wait(timeout=10):
                raise TimeoutError("late finalize was not released")
            return original_consume(*args, **kwargs)
        except BaseException as error:  # noqa: BLE001 - asserted below
            consume_errors.append(error)
            raise
        finally:
            consume_finished.set()

    def capture_abort(*args: Any, **kwargs: Any) -> Any:
        result = original_abort(*args, **kwargs)
        abort_results.append(result)
        abort_finished.set()
        return result

    monkeypatch.setattr(managed_input_upload, "consume_upload_reservation", consume_after_abort)
    monkeypatch.setattr(managed_input_upload, "abort_upload", capture_abort)

    async def race() -> None:
        task = asyncio.create_task(
            managed_input_api._stream_upload(
                request,
                adapter["id"],
                Principal(kind="superadmin"),
            )
        )
        try:
            assert await asyncio.to_thread(consume_entered.wait, 5)
            task.cancel()
            assert await asyncio.to_thread(abort_finished.wait, 5)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            release_consume.set()
            assert await asyncio.to_thread(consume_finished.wait, 5)

    asyncio.run(race())
    assert len(abort_results) == 1
    assert abort_results[0].outcome == "cancelled"
    assert abort_results[0].cleanup_authorized is True
    assert len(consume_errors) == 1
    assert isinstance(consume_errors[0], HTTPException)

    store = LocalFileArtifactStore(store_root)
    with session_factory() as session:
        artifact = session.scalar(
            select(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == adapter["id"])
        )
        assert artifact is not None
        assert artifact.status == "DELETED"
        assert store.stat(artifact.storage_key) is None
        assert not store.part_path(artifact.storage_key).exists()
        reservation = session.get(ManagedInputUploadReservation, artifact.upload_reservation_id)
        assert reservation is not None
        assert reservation.status == ManagedInputReservationStatus.CANCELLED
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == capacity.reserved_bytes == 0


def test_abort_result_allows_idempotent_cleanup_only_for_deleted_upload(
    api_client: Any,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _create_task(api_client, "review-abort-outcome")
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store = LocalFileArtifactStore(tmp_path / "store")
    with session_factory() as session:
        state = managed_input_upload.begin_upload(
            session,
            adapter["id"],
            original_filename="review.txt",
            content_type="text/plain",
            store=store,
        )
    with store.put_part(state.storage_key) as part:
        part.write(b"first")
    store.commit(state.storage_key)

    with session_factory() as session:
        first = managed_input_upload.abort_upload(
            session,
            adapter["id"],
            state.upload_session_id,
            store=store,
        )
    assert first.outcome == "cancelled"
    assert first.cleanup_authorized is True
    assert store.stat(state.storage_key) is None

    with store.put_part(state.storage_key) as part:
        part.write(b"residual")
    store.commit(state.storage_key)
    with session_factory() as session:
        repeated = managed_input_upload.abort_upload(
            session,
            adapter["id"],
            state.upload_session_id,
            store=store,
        )
    assert repeated.outcome == "cleanup_retry"
    assert repeated.cleanup_authorized is True
    assert store.stat(state.storage_key) is None


def test_db_compensation_failure_deletes_only_part_not_published_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalFileArtifactStore(tmp_path / "store")
    storage_key = store.new_storage_key()
    with store.put_part(storage_key) as part:
        part.write(b"published")
    store.commit(storage_key)
    with store.put_part(storage_key) as part:
        part.write(b"partial")
    state = UploadSessionState(
        adapter_id=1,
        artifact_id=1,
        reservation_id=1,
        upload_session_id="review-db-failure",
        storage_key=storage_key,
        reserved_bytes=64 * 1024,
    )

    async def fail_db(_operation: Any) -> Any:
        raise RuntimeError("simulated compensation database failure")

    monkeypatch.setattr(managed_input_api, "_run_db", fail_db)
    asyncio.run(
        managed_input_api._compensate_upload(
            state,
            store,
            error_code="input_upload_failed",
        )
    )

    assert not store.part_path(storage_key).exists()
    assert store.stat(storage_key) is not None
