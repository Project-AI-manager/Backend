"""Vector RAG tests without a real Qdrant service."""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncGenerator, Sequence
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.models.knowledge import KbChunk, KbDocument
from app.models.tenant import Tenant
from app.services.ml.memory import VectorMemoryRetriever
from app.services.rag.embeddings import (
    VECTOR_DIM,
    EmbeddingProviderRequestError,
    LocalEmbedding,
    LocalMLEmbedding,
    OpenAICompatibleEmbedding,
)
from app.services.rag.vector_store import (
    QdrantVectorStore,
    VectorPoint,
    VectorSearchHit,
    _embedded_vector_store,
    get_vector_store,
)

TENANT_A = uuid.UUID("55555555-5555-4555-8555-555555555501")
TENANT_B = uuid.UUID("55555555-5555-4555-8555-555555555502")


class FakeVectorStore:
    def __init__(self, hits: list[VectorSearchHit]) -> None:
        self.hits = hits
        self.searched_tenant_id: uuid.UUID | None = None
        self.upserted: list[VectorPoint] = []

    async def ensure_collection(self) -> None:
        return None

    async def upsert_chunks(self, points: Sequence[VectorPoint]) -> None:
        self.upserted.extend(points)

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        vector: list[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        self.searched_tenant_id = tenant_id
        return self.hits[:limit]

    async def delete_document(self, *, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
        return None


class FailingVectorStore(FakeVectorStore):
    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        vector: list[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        raise RuntimeError("qdrant unavailable")


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for table in (
            Tenant.__table__,
            KbDocument.__table__,
            KbChunk.__table__,
        ):
            await conn.run_sync(create_table, table)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_local_embedding_is_deterministic_non_zero_and_normalized() -> None:
    embedder = LocalEmbedding()

    first, second, empty = await embedder.embed(["Telegram setup", "Telegram setup", ""])

    assert first == second
    assert len(first) == VECTOR_DIM
    assert any(value != 0 for value in first)
    assert empty == [0.0] * VECTOR_DIM
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0, abs_tol=0.0001)


@pytest.mark.asyncio
async def test_local_ml_embedding_adds_e5_query_and_passage_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    class FakeModel:
        def embed(self, texts: list[str]) -> list[list[float]]:
            captured.append(texts)
            return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(
        "app.services.rag.embeddings._load_fastembed_model",
        lambda model, cache_dir: FakeModel(),
    )
    embedder = LocalMLEmbedding(
        model="intfloat/multilingual-e5-large",
        dimension=3,
        cache_dir="cache",
    )

    await embedder.embed_queries(["цена доставки"])
    await embedder.embed_passages(["Доставка стоит 500 рублей"])

    assert captured == [
        ["query: цена доставки"],
        ["passage: Доставка стоит 500 рублей"],
    ]


@pytest.mark.asyncio
async def test_openai_compatible_embeddings_use_contract_and_restore_input_order() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["payload"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            },
        )

    embedder = OpenAICompatibleEmbedding(
        base_url="https://embeddings.example.test/v1/",
        api_key="secret-token",
        model="multilingual-e5",
        dimension=3,
        timeout_sec=2,
        transport=httpx.MockTransport(handler),
    )

    vectors = await embedder.embed(["first", "second"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert captured["url"] == "https://embeddings.example.test/v1/embeddings"
    assert captured["auth"] == "Bearer secret-token"
    assert '"model":"multilingual-e5"' in str(captured["payload"])


@pytest.mark.asyncio
async def test_openai_compatible_embeddings_reject_dimension_mismatch() -> None:
    embedder = OpenAICompatibleEmbedding(
        base_url="https://embeddings.example.test/v1",
        api_key="secret-token",
        model="small",
        dimension=3,
        timeout_sec=2,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
            )
        ),
    )

    with pytest.raises(EmbeddingProviderRequestError, match="dimension mismatch"):
        await embedder.embed(["wrong dimension"])


class FakeQdrantClient:
    def __init__(self, *, collection_size: int) -> None:
        self.collection_size = collection_size

    async def collection_exists(self, collection: str) -> bool:
        return True

    async def get_collection(self, collection: str) -> object:
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=self.collection_size))
            )
        )


class CreatingQdrantClient:
    def __init__(self) -> None:
        self.vectors_config: Any = None

    async def collection_exists(self, collection: str) -> bool:
        return False

    async def create_collection(self, *, collection_name: str, vectors_config: object) -> None:
        self.vectors_config = vectors_config


@pytest.mark.asyncio
async def test_qdrant_rejects_existing_collection_with_wrong_dimension() -> None:
    store = QdrantVectorStore(
        url="http://qdrant.test",
        collection="knowledge",
        vector_size=3,
        client=FakeQdrantClient(collection_size=2),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="dimension is 2"):
        await store.ensure_collection()


@pytest.mark.asyncio
async def test_qdrant_creates_collection_with_configured_dimension() -> None:
    client = CreatingQdrantClient()
    store = QdrantVectorStore(
        url="http://qdrant.test",
        collection="knowledge",
        vector_size=3,
        client=client,  # type: ignore[arg-type]
    )

    await store.ensure_collection()

    assert client.vectors_config.size == 3


@pytest.mark.asyncio
async def test_vector_store_supports_embedded_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "QDRANT_ENABLED", True)
    monkeypatch.setattr(settings, "QDRANT_URL", ":memory:")

    store = get_vector_store()
    same_store = get_vector_store()

    assert isinstance(store, QdrantVectorStore)
    assert same_store is store
    await store.ensure_collection()
    await store.client.close()
    _embedded_vector_store.cache_clear()


@pytest.mark.asyncio
async def test_vector_retriever_uses_qdrant_hits_and_sql_tenant_filter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    chunk_a_id, chunk_b_id, archived_chunk_id = await _seed_vector_data(session_factory)
    vector_store = FakeVectorStore(
        [
            VectorSearchHit(chunk_id=chunk_b_id, score=0.99),
            VectorSearchHit(chunk_id=archived_chunk_id, score=0.98),
            VectorSearchHit(chunk_id=chunk_a_id, score=0.97),
        ]
    )

    async with session_factory() as session:
        retriever = VectorMemoryRetriever(session=session, vector_store=vector_store)
        snippets = await retriever.retrieve(
            tenant_id=TENANT_A,
            query="activation handbook",
            limit=3,
        )

    assert vector_store.searched_tenant_id == TENANT_A
    assert len(snippets) == 1
    assert snippets[0].id == str(chunk_a_id)
    assert snippets[0].source == "qdrant"
    assert snippets[0].score == 0.97
    assert snippets[0].title == "Alpha handbook"


@pytest.mark.asyncio
async def test_vector_retriever_falls_back_to_sql_when_vector_store_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_vector_data(session_factory)

    async with session_factory() as session:
        retriever = VectorMemoryRetriever(
            session=session,
            vector_store=FailingVectorStore([]),
        )
        snippets = await retriever.retrieve(
            tenant_id=TENANT_A,
            query="telegram activation",
            limit=2,
        )

    assert len(snippets) == 1
    assert snippets[0].source == "knowledge-base"
    assert snippets[0].title == "Alpha handbook"


async def _seed_vector_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        session.add_all(
            [
                Tenant(id=TENANT_A, name="Alpha", slug="alpha-vector", status="active"),
                Tenant(id=TENANT_B, name="Beta", slug="beta-vector", status="active"),
            ]
        )
        ready_a = KbDocument(
            tenant_id=TENANT_A,
            title="Alpha handbook",
            source_type="manual",
            status="ready",
            version=1,
        )
        ready_b = KbDocument(
            tenant_id=TENANT_B,
            title="Beta handbook",
            source_type="manual",
            status="ready",
            version=1,
        )
        archived = KbDocument(
            tenant_id=TENANT_A,
            title="Archived handbook",
            source_type="manual",
            status="archived",
            version=1,
        )
        session.add_all([ready_a, ready_b, archived])
        await session.flush()
        chunk_a = KbChunk(
            tenant_id=TENANT_A,
            document_id=ready_a.id,
            text="Telegram activation takes fifteen minutes.",
            position=0,
            token_count=5,
            vector_id="alpha-vector",
            tags={"topic": "telegram"},
            version=1,
        )
        chunk_b = KbChunk(
            tenant_id=TENANT_B,
            document_id=ready_b.id,
            text="Beta private pricing must not leak.",
            position=0,
            token_count=6,
            vector_id="beta-vector",
            tags={"topic": "telegram"},
            version=1,
        )
        archived_chunk = KbChunk(
            tenant_id=TENANT_A,
            document_id=archived.id,
            text="Old archived Telegram rule.",
            position=0,
            token_count=4,
            vector_id="archived-vector",
            tags={"topic": "telegram"},
            version=1,
        )
        session.add_all([chunk_a, chunk_b, archived_chunk])
        await session.commit()
        return chunk_a.id, chunk_b.id, archived_chunk.id
