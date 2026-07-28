"""Vector store abstraction for knowledge-base chunks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings


@dataclass(frozen=True)
class VectorPoint:
    chunk_id: UUID
    vector_id: str
    tenant_id: UUID
    document_id: UUID
    title: str
    text: str
    tags: dict[str, str]
    version: int
    vector: list[float]


@dataclass(frozen=True)
class VectorSearchHit:
    chunk_id: UUID
    score: float
    vector_id: str = ""


class VectorStore(Protocol):
    async def ensure_collection(self) -> None: ...

    async def upsert_chunks(self, points: Sequence[VectorPoint]) -> None: ...

    async def search(
        self,
        *,
        tenant_id: UUID,
        vector: list[float],
        limit: int,
    ) -> list[VectorSearchHit]: ...

    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> None: ...


class QdrantVectorStore:
    """Async Qdrant implementation kept behind a tiny project-owned protocol."""

    def __init__(
        self,
        *,
        url: str | None = None,
        path: str | None = None,
        collection: str,
        vector_size: int,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self.collection = collection
        self.vector_size = vector_size
        self.client = client or AsyncQdrantClient(url=url, path=path)

    async def ensure_collection(self) -> None:
        exists = await self.client.collection_exists(self.collection)
        if exists:
            collection = await self.client.get_collection(self.collection)
            actual_size = _collection_vector_size(collection)
            if actual_size is None:
                raise RuntimeError(
                    f"Qdrant collection '{self.collection}' has no single dense vector config"
                )
            if actual_size != self.vector_size:
                raise RuntimeError(
                    f"Qdrant collection '{self.collection}' dimension is {actual_size}, "
                    f"but EMBEDDING_DIMENSION is {self.vector_size}. "
                    "Create a new collection or reindex it with the configured dimension."
                )
            return
        await self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=self.vector_size,
                distance=models.Distance.COSINE,
            ),
        )

    async def upsert_chunks(self, points: Sequence[VectorPoint]) -> None:
        if not points:
            return
        invalid = [point for point in points if len(point.vector) != self.vector_size]
        if invalid:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.vector_size}, "
                f"got {len(invalid[0].vector)}"
            )
        await self.ensure_collection()
        await self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=str(point.chunk_id),
                    vector=point.vector,
                    payload={
                        "tenant_id": str(point.tenant_id),
                        "document_id": str(point.document_id),
                        "chunk_id": str(point.chunk_id),
                        "vector_id": point.vector_id,
                        "title": point.title,
                        "text": point.text,
                        "tags": point.tags,
                        "version": point.version,
                    },
                )
                for point in points
            ],
            wait=True,
        )

    async def search(
        self,
        *,
        tenant_id: UUID,
        vector: list[float],
        limit: int,
    ) -> list[VectorSearchHit]:
        if limit <= 0:
            return []
        if len(vector) != self.vector_size:
            raise ValueError(
                f"Query vector dimension mismatch: expected {self.vector_size}, got {len(vector)}"
            )
        await self.ensure_collection()
        response = await self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id",
                        match=models.MatchValue(value=str(tenant_id)),
                    )
                ]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        hits: list[VectorSearchHit] = []
        for point in response.points:
            payload = _payload_dict(point.payload)
            chunk_id = _uuid_or_none(payload.get("chunk_id") or point.id)
            if chunk_id is None:
                continue
            hits.append(
                VectorSearchHit(
                    chunk_id=chunk_id,
                    score=float(point.score),
                    vector_id=str(payload.get("vector_id") or ""),
                )
            )
        return hits

    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> None:
        await self.ensure_collection()
        await self.client.delete(
            collection_name=self.collection,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id",
                        match=models.MatchValue(value=str(tenant_id)),
                    ),
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchValue(value=str(document_id)),
                    ),
                ]
            ),
            wait=True,
        )


def get_vector_store() -> VectorStore | None:
    if not settings.QDRANT_ENABLED:
        return None
    configured_url = settings.QDRANT_URL.strip()
    local_path = settings.QDRANT_LOCAL_PATH.strip()
    if configured_url.lower() in {"local", "embedded", ":memory:"}:
        return _embedded_vector_store(
            ":memory:" if configured_url.lower() == ":memory:" else local_path,
            collection=settings.QDRANT_COLLECTION,
            vector_size=settings.EMBEDDING_DIMENSION,
        )
    return QdrantVectorStore(
        url=configured_url,
        collection=settings.QDRANT_COLLECTION,
        vector_size=settings.EMBEDDING_DIMENSION,
    )


@lru_cache(maxsize=4)
def _embedded_vector_store(
    path: str,
    *,
    collection: str,
    vector_size: int,
) -> QdrantVectorStore:
    """Keep one local client per process because embedded storage holds a file lock."""
    return QdrantVectorStore(
        path=path,
        collection=collection,
        vector_size=vector_size,
    )


def _collection_vector_size(collection: Any) -> int | None:
    vectors = collection.config.params.vectors
    size = getattr(vectors, "size", None)
    return int(size) if isinstance(size, int) else None


def _payload_dict(payload: Any) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
