"""Real Reader pins and PostgreSQL sessions under request-loop cancellation."""

import asyncio
import base64
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from dlr.control.ai.attachment_documents import DocumentConverterClient, DocumentResult
from dlr.control.ai.attachment_store import AiAttachmentStore
from dlr.control.schemas.ai import AiAttachment, AiAttachmentReference
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import RunContext
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import sessionmaker

from guards import Rejected
from owned_work import WorkOwner


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


def prepare(store: AiAttachmentStore, user: int) -> tuple[str, AiAttachmentReference]:
    scope = str(uuid4())
    attachment = AiAttachment(
        filename="source.txt",
        content_type="text/plain",
        data_base64=base64.b64encode(f"material for user {user}".encode()).decode(),
    )
    result = store.prepare(
        ("account", user), user, scope, attachment, deadline=time.monotonic() + 3
    )
    return scope, AiAttachmentReference(
        attachment_ref=result.attachment_ref,
        sha256=result.sha256,
        representation_revision=result.representation_revision,
    )


def read_once(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if any(isinstance(message, ModelResponse) for message in messages):
        return ModelResponse(parts=[TextPart("complete")])
    return ModelResponse(parts=[ToolCallPart("read_owned", {}, "owned-read")])


def test_cancelled_sync_tool_keeps_real_reader_pin_beyond_closed_request_loop(
    tmp_path: Path,
) -> None:
    owner = WorkOwner(1)
    store = AiAttachmentStore(tmp_path / "store", TextConverter())
    entered, release = threading.Event(), threading.Event()
    scope, ref = prepare(store, 1)
    directory = store._records[ref.attachment_ref].directory

    def read() -> str:
        with store.reader(("account", 1), 1, scope, [ref]) as reader:
            assert reader.overview()["coverage"]["directory_only"]
            entered.set()
            assert release.wait(3)
            return "late result must not become a candidate"

    async def read_owned() -> str:
        return await owner.result(owner.submit(read))

    agent = Agent(FunctionModel(read_once), tools=[read_owned], retries=0)

    async def scenario() -> None:
        task = asyncio.create_task(agent.run("Synthetic"))
        deadline = time.monotonic() + 2
        while not entered.is_set():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(scenario())
        store.release(("account", 1), 1, scope)
        assert directory.exists()
        assert store._records[ref.attachment_ref].pins == 1
        with pytest.raises(Rejected, match="work_capacity"):
            owner.submit(lambda: None)
        release.set()
        owner.close()
        assert ref.attachment_ref not in store._records
        assert not directory.exists()
    finally:
        release.set()
        owner.close()
        store.close()


@dataclass(frozen=True)
class Prepared:
    user: int
    scope: str
    ref: AiAttachmentReference
    credential: str


def test_two_users_real_postgres_short_sessions_reader_and_credential_isolation(
    tmp_path: Path,
) -> None:
    url = os.environ.get("B0_TEST_DATABASE_URL")
    if not url:
        pytest.skip("B0_TEST_DATABASE_URL is required for real PostgreSQL evidence")
    engine = create_engine(url)
    assert engine.dialect.name == "postgresql"
    factory = sessionmaker(engine)
    metadata = MetaData()
    acl = Table(
        "b0_scope_" + uuid4().hex,
        metadata,
        Column("user_id", Integer, primary_key=True),
        Column("allowed", Boolean, nullable=False),
        Column("credential", String, nullable=False),
    )
    metadata.create_all(engine)
    owner = WorkOwner(2)
    store = AiAttachmentStore(tmp_path / "store", TextConverter())
    entered, release = threading.Event(), threading.Event()
    observed: list[tuple[int, str, int]] = []
    main_thread = threading.get_ident()
    try:
        with engine.begin() as connection:
            connection.execute(
                acl.insert(),
                [
                    {"user_id": 1, "allowed": True, "credential": "synthetic-key-a"},
                    {"user_id": 2, "allowed": True, "credential": "synthetic-key-b"},
                ],
            )
        snapshots = []
        for user in (1, 2):
            scope, ref = prepare(store, user)
            with factory() as session:
                key = session.scalar(select(acl.c.credential).where(acl.c.user_id == user))
                assert isinstance(key, str)
                snapshots.append(Prepared(user, scope, ref, key))

        def read(prepared: Prepared) -> str:
            def check() -> None:
                # A fresh execution-thread session, not the preparation thread's
                # identity map or transaction, observes committed revocation.
                assert threading.get_ident() != main_thread
                with factory() as session:
                    allowed = session.scalar(
                        select(acl.c.allowed).where(acl.c.user_id == prepared.user)
                    )
                    observed.append((prepared.user, prepared.credential, threading.get_ident()))
                    if not allowed:
                        raise Rejected("permission", "revoked")

            with store.reader(
                ("account", prepared.user),
                prepared.user,
                prepared.scope,
                [prepared.ref],
                permission_check=check,
            ) as reader:
                if prepared.user == 1:
                    entered.set()
                    assert release.wait(3)
                result = reader.read_page(prepared.ref.attachment_ref, 1, "text", timeout_seconds=2)
                return str(result.data)

        async def read_owned(ctx: RunContext[Prepared]) -> str:
            return await owner.result(owner.submit(lambda: read(ctx.deps)))

        agent = Agent(FunctionModel(read_once), tools=[read_owned], deps_type=Prepared, retries=0)

        async def scenario() -> None:
            task_a = asyncio.create_task(agent.run("Synthetic a", deps=snapshots[0]))
            deadline = time.monotonic() + 2
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            try:
                result_b = await agent.run("Synthetic b", deps=snapshots[1])
                assert result_b.output == "complete"
                with engine.begin() as connection:
                    connection.execute(
                        acl.update()
                        .where(acl.c.user_id == 1)
                        .values(allowed=False, credential="rotated")
                    )
                release.set()
                with pytest.raises(Rejected, match="permission:revoked"):
                    await task_a
            finally:
                release.set()
                if not task_a.done():
                    await task_a

        asyncio.run(scenario())
        assert {row[:2] for row in observed} == {(1, "synthetic-key-a"), (2, "synthetic-key-b")}
        assert all(record.pins == 0 for record in store._records.values())
        assert engine.pool.checkedout() == 0
    finally:
        release.set()
        owner.close()
        store.close()
        metadata.drop_all(engine)
        engine.dispose()
