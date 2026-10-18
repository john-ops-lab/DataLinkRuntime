"""Closed generations cannot consume another owner's accepted cleanup capacity."""

import concurrent.futures
import time
from pathlib import Path
from uuid import uuid4

import pytest

from dlr.control.ai import attachment_store
from dlr.control.ai.attachment_documents import DocumentConversionError
from dlr.control.ai.attachment_store import MAX_SCOPES, AiAttachmentStore
from dlr.control.schemas.ai import AiAttachmentReference
from test_issue202_attachment_store import Converter, attachment


@pytest.fixture
def store(tmp_path: Path):
    client = Converter()
    value = AiAttachmentStore(tmp_path / "owned", client, ttl=60)
    yield value, client
    value.close()


def prepare_for(value, owner, adapter_id=10):
    scope = uuid4()
    result = value.prepare(owner, adapter_id, scope, attachment(), deadline=time.monotonic() + 5)
    ref = AiAttachmentReference(
        attachment_ref=result.attachment_ref,
        sha256=result.sha256,
        representation_revision=result.representation_revision,
    )
    return scope, result, ref


def test_owner_exhaustion_does_not_block_another_owners_release(store) -> None:
    value, _client = store
    other = ("account", 2)
    scope, result, ref = prepare_for(value, other)
    directory = value._records[result.attachment_ref].directory
    refused = 0
    # Trying the former global limit must exhaust only the caller's namespace,
    # even when it varies Adapter IDs as well as page generations.
    for adapter_id in range(MAX_SCOPES * 4):
        try:
            value.release(("account", 1), adapter_id, uuid4())
        except DocumentConversionError as error:
            assert error.code == "ai_attachment_store_full"
            refused += 1
    with value.reader(other, 10, scope, [ref]) as reader:
        value.release(other, 10, scope)
        assert directory.exists()  # Active pin still owns its bytes.
        with pytest.raises(DocumentConversionError, match="ai_attachment_expired"):
            reader.overview()
    assert refused == MAX_SCOPES * 3
    assert not directory.exists() and value._used_bytes == 0 and not value._records
    assert len(value._closed_scopes) == MAX_SCOPES + 1


def test_owner_limit_covers_new_prepare_and_release_but_keeps_release_idempotent(store) -> None:
    value, _client = store
    owner = ("admin_token", None)
    scopes = [uuid4() for _ in range(MAX_SCOPES)]
    for index, scope in enumerate(scopes):
        value.release(owner, index, scope)
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
        value.release(owner, 999, uuid4())
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
        value.prepare(owner, 999, uuid4(), attachment(), deadline=time.monotonic() + 3)
    value.release(owner, 0, scopes[0])
    assert len(value._closed_scopes) == MAX_SCOPES
    # An unexpired closed generation stays closed even at quota, rather than
    # evicting an old tombstone to make room for a late upload.
    with pytest.raises(DocumentConversionError, match="ai_attachment_cancelled"):
        value.prepare(owner, 0, scopes[0], attachment(), deadline=time.monotonic() + 3)
    scope, result, _ref = prepare_for(value, ("account", 1), 999)
    value.release(("account", 1), 999, scope)
    assert result.attachment_ref not in value._records


def test_global_capacity_reserves_release_for_every_accepted_scope(store) -> None:
    value, _client = store
    owner = ("account", 5)
    scope, result, _ref = prepare_for(value, owner)
    accepted = 0
    for user_id in range(1, 5):
        for index in range(MAX_SCOPES):
            try:
                value.release(("account", user_id), index, uuid4())
                accepted += 1
            except DocumentConversionError as error:
                assert error.code == "ai_attachment_store_full"
    assert accepted == MAX_SCOPES * 4 - 1
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
        value.release(("account", 6), 10, uuid4())
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
        value.prepare(("account", 6), 10, uuid4(), attachment(), deadline=time.monotonic() + 3)
    value.release(owner, 10, scope)
    value.release(owner, 10, scope)
    assert not value._records and value._used_bytes == 0
    assert len(value._closed_scopes) == MAX_SCOPES * 4
    assert result.attachment_ref not in value._records


def test_active_scope_counts_towards_its_owners_capacity_and_can_be_closed(store) -> None:
    value, _client = store
    owner = ("account", 1)
    scope, result, _ref = prepare_for(value, owner)
    for index in range(MAX_SCOPES - 1):
        value.release(owner, index + 20, uuid4())
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
        value.release(owner, 999, uuid4())
    value.release(owner, 10, scope)
    assert result.attachment_ref not in value._records and value._used_bytes == 0
    assert len(value._closed_scopes) == MAX_SCOPES


def test_scope_capacity_recovers_after_ttl_without_evicting_live_tombstones(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    value, _client = store
    owner = ("account", 1)
    now = [100.0]
    monkeypatch.setattr(attachment_store.time, "monotonic", lambda: now[0])
    scope = uuid4()
    value.release(owner, 10, scope)
    for index in range(MAX_SCOPES - 1):
        value.release(owner, index + 20, uuid4())
    now[0] = 159.0
    with pytest.raises(DocumentConversionError, match="ai_attachment_store_full"):
        value.release(owner, 999, uuid4())
    with pytest.raises(DocumentConversionError, match="ai_attachment_cancelled"):
        value.prepare(owner, 10, scope, attachment(), deadline=now[0] + 3)
    now[0] = 160.0
    value.sweep()
    assert not value._closed_scopes
    value.release(owner, 999, uuid4())
    assert len(value._closed_scopes) == 1


def test_concurrent_unknown_releases_share_one_owner_quota(store) -> None:
    value, _client = store

    def close(index):
        try:
            value.release(("account", 1), index, uuid4())
            return True
        except DocumentConversionError as error:
            assert error.code == "ai_attachment_store_full"
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(close, range(MAX_SCOPES + 32)))
    assert sum(outcomes) == MAX_SCOPES and len(value._closed_scopes) == MAX_SCOPES
    scope, result, _ref = prepare_for(value, ("account", 2))
    value.release(("account", 2), 10, scope)
    assert result.attachment_ref not in value._records


def test_owner_exhaustion_does_not_block_another_owners_active_cancel(store) -> None:
    value, client = store
    for index in range(MAX_SCOPES):
        value.release(("account", 1), index, uuid4())
    client.block = True
    scope = uuid4()

    def prepare():
        with pytest.raises(DocumentConversionError, match="ai_attachment_cancelled"):
            value.prepare(("account", 2), 10, scope, attachment(), deadline=time.monotonic() + 3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(prepare)
        assert client.entered.wait(1)
        value.release(("account", 2), 10, scope)
        pending.result(timeout=1)
    assert client.cancelled and not value._records and value._used_bytes == 0
