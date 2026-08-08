"""Worker contracts and retry safety without a real Redis or Qdrant."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.models.knowledge import KbChunk, KbDocument
from app.schemas.channels import ChannelWebhookResponse
from app.services.rag.vector_store import VectorPoint
from app.workers.queue import enqueue_document_reindex, enqueue_inbound_message
from app.workers.worker import process_inbound_message, reindex_document


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(cast(Table, KbDocument.__table__).create)
        await connection.run_sync(cast(Table, KbChunk.__table__).create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: dict[str, tuple[str, tuple[Any, ...]]] = {}

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> object | None:
        job_id = str(kwargs["_job_id"])
        if job_id in self.jobs:
            return None
        self.jobs[job_id] = (function, args)
        return object()


class FakeVectorStore:
    def __init__(self) -> None:
        self.points: dict[uuid.UUID, VectorPoint] = {}
        self.delete_calls = 0

    async def delete_document(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self.delete_calls += 1
        self.points = {
            key: point
            for key, point in self.points.items()
            if point.tenant_id != tenant_id or point.document_id != document_id
        }

    async def upsert_chunks(self, points: list[VectorPoint]) -> None:
        self.points.update({point.chunk_id: point for point in points})


async def test_queue_contract_deduplicates_stable_job_ids() -> None:
    queue = FakeQueue()
    message_id = uuid.uuid4()
    document_id = uuid.uuid4()

    assert await enqueue_inbound_message(queue, message_id) is True
    assert await enqueue_inbound_message(queue, message_id) is False
    assert await enqueue_document_reindex(queue, document_id) is True
    assert await enqueue_document_reindex(queue, document_id) is False
    assert queue.jobs[f"inbound:{message_id}"] == (
        "process_inbound_message",
        (str(message_id),),
    )


async def test_reindex_worker_is_retry_safe(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            KbDocument(
                id=document_id,
                tenant_id=tenant_id,
                title="FAQ",
                source_type="manual",
                status="ready",
                version=1,
            )
        )
        session.add(
            KbChunk(
                id=chunk_id,
                tenant_id=tenant_id,
                document_id=document_id,
                text="Telegram setup",
                position=0,
                token_count=2,
                vector_id=f"kb:{document_id}:0",
                tags={"topic": "telegram"},
                version=1,
            )
        )
        await session.commit()

    store = FakeVectorStore()
    monkeypatch.setattr("app.services.knowledge.get_vector_store", lambda: store)
    ctx = {"session_factory": session_factory}

    first = await reindex_document(ctx, str(document_id))
    second = await reindex_document(ctx, str(document_id))

    assert first == {"document_id": str(document_id), "status": "indexed", "chunks_count": 1}
    assert second == first
    assert store.delete_calls == 2
    assert list(store.points) == [chunk_id]
    async with session_factory() as session:
        stored_document = await session.get(KbDocument, document_id)
        assert stored_document is not None
        assert stored_document.status == "ready"


async def test_reindex_worker_treats_missing_document_as_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = uuid.uuid4()
    result = await reindex_document({"session_factory": session_factory}, str(document_id))
    assert result == {
        "document_id": str(document_id),
        "status": "missing",
        "chunks_count": 0,
    }


async def test_inbound_worker_uses_persisted_message_contract(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_id = uuid.uuid4()
    seen: list[uuid.UUID] = []

    async def fake_process(session: AsyncSession, parsed_message_id: uuid.UUID):
        seen.append(parsed_message_id)
        return ChannelWebhookResponse(ok=True, inbound_message_id=parsed_message_id)

    monkeypatch.setattr(
        "app.workers.worker.process_channel_inbound_message",
        fake_process,
    )

    result = await process_inbound_message(
        {"session_factory": session_factory},
        str(message_id),
    )

    assert seen == [message_id]
    assert result["inbound_message_id"] == str(message_id)
