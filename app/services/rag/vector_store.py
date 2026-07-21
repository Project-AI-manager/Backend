"""Vector store abstraction for knowledge-base chunks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings
from app.services.rag.embeddings import VECTOR_DIM


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
        url: str,
        collection: str,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self.collection = collection
        self.client = client or AsyncQdrantClient(url=url)

    async def ensure_collection(self) -> None:
        exists = await self.client.collection_exists(self.collection)
        if exists:
            return
        await self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(size=VECTOR_DIM, distance=models.Distance.COSINE),
        )

    async def upsert_chunks(self, points: Sequence[VectorPoint]) -> None:
        if not points:
            return
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
    return QdrantVectorStore(url=settings.QDRANT_URL, collection=settings.QDRANT_COLLECTION)


def _payload_dict(payload: Any) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None
