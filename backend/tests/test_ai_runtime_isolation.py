"""Two live request identities keep frozen secrets and scoped Readers isolated."""

import base64
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, select

from dlr.control.ai.attachment_documents import DocumentConverterClient, DocumentResult
from dlr.control.ai.attachment_store import AiAttachmentStore
from dlr.control.ai.provider_config import ProviderSnapshot, get_provider
from dlr.control.ai.runtime.resources import ToolResourceFactory
from dlr.control.ai.tools import ToolExecutionContext
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference, AiSettingDraft


class TextConverter(DocumentConverterClient):
    def __init__(self) -> None:
        super().__init__("http://unused.invalid")

    def prepare(
        self, filename: str, content_type: str, data: bytes, **kwargs: Any
    ) -> DocumentResult:
        return DocumentResult(
            "success",
            hashlib.sha256(data).hexdigest(),
            "synthetic-v1",
            1,
            pages=[{"page": 1, "status": "available"}],
        )

    def read_page(
        self, filename: str, content_type: str, data: bytes, **kwargs: Any
    ) -> DocumentResult:
        return DocumentResult(
            "success",
            hashlib.sha256(data).hexdigest(),
            "synthetic-v1",
            1,
            data={"text": data.decode()},
        )


def test_two_users_postgres_factories_keep_snapshot_reader_and_revocation_isolated(
    session_factory: Any,
    tmp_path: Any,
) -> None:
    engine = session_factory.kw["bind"]
    assert engine.dialect.name == "postgresql"
    metadata = MetaData()
    acl = Table(
        "issue213_acl_" + uuid4().hex,
        metadata,
        Column("user_id", Integer, primary_key=True),
        Column("allowed", Boolean, nullable=False),
        Column("credential", String, nullable=False),
    )
    metadata.create_all(engine)
    store = AiAttachmentStore(tmp_path / "two-users", TextConverter())
    entered, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    observed: list[tuple[int, str | None, int]] = []
    records: dict[int, tuple[ToolResourceFactory, ProviderSnapshot, AiAttachmentReference]] = {}
    try:
        with engine.begin() as writer:
            writer.execute(
                acl.insert(),
                [
                    {"user_id": user, "allowed": True, "credential": f"synthetic-key-{user}"}
                    for user in (1, 2)
                ],
            )
        for user in (1, 2):
            scope = str(uuid4())
            ready = store.prepare(
                ("account", user),
                user,
                scope,
                AiAttachment(
                    filename="source.txt",
                    content_type="text/plain",
                    data_base64=base64.b64encode(f"material for user {user}".encode()).decode(),
                ),
                deadline=time.monotonic() + 3,
            )
            ref = AiAttachmentReference(
                **{
                    name: getattr(ready, name)
                    for name in ("attachment_ref", "sha256", "representation_revision")
                }
            )
            with session_factory() as session:
                key = session.scalar(select(acl.c.credential).where(acl.c.user_id == user))
            assert isinstance(key, str)
            snapshot = ProviderSnapshot.create(
                AiSettingDraft(
                    provider="custom_openai_compatible",
                    base_url="https://fixture.invalid",
                    model="fixture",
                ),
                get_provider("custom_openai_compatible"),
                key,
            )

            def check(user: int = user) -> None:
                assert threading.get_ident() != main_thread
                with session_factory() as session:
                    allowed = session.scalar(select(acl.c.allowed).where(acl.c.user_id == user))
                if not allowed:
                    raise PermissionError("committed revocation")

            def reader(
                user: int = user, scope: str = scope, ref: Any = ref, check: Any = check
            ) -> Any:
                return store.reader(("account", user), user, scope, [ref], permission_check=check)

            records[user] = (
                ToolResourceFactory(
                    session_factory,
                    ToolExecutionContext(secret_values=[key]),
                    reader_factory=reader,
                    permission_check=check,
                ),
                snapshot,
                ref,
            )

        def read(user: int) -> str:
            factory, snapshot, ref = records[user]
            with factory.open() as context:
                assert context.session is not None and context.session.scalar(select(1)) == 1
                assert context.attachment_reader is not None
                assert context.secret_values == [snapshot.api_key]
                observed.append((user, snapshot.api_key, threading.get_ident()))
                if user == 1:
                    entered.set()
                    assert release.wait(3)
                result = context.attachment_reader.read_page(
                    ref.attachment_ref, 1, "text", timeout_seconds=2
                )
                return str(result.data["text"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(read, 1)
            try:
                assert entered.wait(2)
                assert pool.submit(read, 2).result(timeout=2) == "material for user 2"
                with engine.begin() as writer:
                    writer.execute(
                        acl.update()
                        .where(acl.c.user_id == 1)
                        .values(allowed=False, credential="rotated-after-request-start")
                    )
                release.set()
                with pytest.raises(PermissionError, match="committed revocation"):
                    first.result(timeout=2)
            finally:
                release.set()
        assert {row[:2] for row in observed} == {(1, "synthetic-key-1"), (2, "synthetic-key-2")}
        assert all(row[2] != main_thread for row in observed)
        assert all(record.pins == 0 for record in store._records.values())
        assert engine.pool.checkedout() == 0
    finally:
        release.set()
        store.close()
        metadata.drop_all(engine)
