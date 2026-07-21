"""Tenant-aware company memory retrieval without external ML services."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.models.knowledge import KbChunk, KbDocument
from app.models.tenant import TenantAIConfig
from app.services.ml.contracts import MemorySnippet
from app.services.rag.embeddings import EmbeddingProvider, get_embedder
from app.services.rag.vector_store import VectorSearchHit, VectorStore, get_vector_store

TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")
STOP_WORDS = {
    "а",
    "без",
    "в",
    "во",
    "год",
    "да",
    "до",
    "есть",
    "и",
    "как",
    "какая",
    "какие",
    "какой",
    "ли",
    "на",
    "не",
    "по",
    "про",
    "с",
    "у",
    "условия",
    "что",
    "это",
}


class MemoryRetriever(ABC):
    @abstractmethod
    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        query: str,
        limit: int = 4,
    ) -> list[MemorySnippet]: ...


def tokenize(text: str) -> Counter[str]:
    return Counter(
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 2 and token.lower() not in STOP_WORDS
    )


class KeywordMemoryRetriever(MemoryRetriever):
    """Deterministic in-memory retriever for unit tests and local experiments."""

    def __init__(self, snippets: list[MemorySnippet] | None = None) -> None:
        self.snippets = default_sales_memory() if snippets is None else snippets

    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        query: str,
        limit: int = 4,
    ) -> list[MemorySnippet]:
        return rank_snippets(self.snippets, query=query, limit=limit)


class DatabaseMemoryRetriever(MemoryRetriever):
    """Retrieve and rank ready knowledge-base chunks inside one tenant."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        query: str,
        limit: int = 4,
    ) -> list[MemorySnippet]:
        result = await self.session.execute(
            select(KbChunk, KbDocument.title)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(
                KbChunk.tenant_id == tenant_id,
                KbDocument.tenant_id == tenant_id,
                KbDocument.status == "ready",
            )
        )
        snippets = [
            MemorySnippet(
                id=str(chunk.id),
                title=title,
                text=chunk.text,
                score=1.0,
                source="knowledge-base",
                tags={str(key): str(value) for key, value in (chunk.tags or {}).items()},
            )
            for chunk, title in result.all()
        ]
        return rank_snippets(snippets, query=query, limit=limit)


class VectorMemoryRetriever(MemoryRetriever):
    """Vector-first retriever with SQL verification and keyword fallback."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        vector_store: VectorStore,
        embedder: EmbeddingProvider | None = None,
        fallback: MemoryRetriever | None = None,
    ) -> None:
        self.session = session
        self.vector_store = vector_store
        self.embedder = embedder or get_embedder()
        self.fallback = fallback or DatabaseMemoryRetriever(session)

    async def retrieve(
        self,
        *,
        tenant_id: UUID,
        query: str,
        limit: int = 4,
    ) -> list[MemorySnippet]:
        try:
            [query_vector] = await self.embedder.embed([query])
            hits = await self.vector_store.search(
                tenant_id=tenant_id,
                vector=query_vector,
                limit=limit,
            )
            snippets = await self._snippets_from_hits(
                tenant_id=tenant_id,
                hits=hits,
                limit=limit,
            )
            if snippets:
                return snippets
        except Exception as exc:  # noqa: BLE001 - retrieval must degrade to SQL fallback.
            log.warning("vector_retrieval_failed", error=str(exc), tenant_id=str(tenant_id))

        return await self.fallback.retrieve(tenant_id=tenant_id, query=query, limit=limit)

    async def _snippets_from_hits(
        self,
        *,
        tenant_id: UUID,
        hits: list[VectorSearchHit],
        limit: int,
    ) -> list[MemorySnippet]:
        chunk_ids = [hit.chunk_id for hit in hits]
        if not chunk_ids:
            return []

        result = await self.session.execute(
            select(KbChunk, KbDocument.title)
            .join(KbDocument, KbDocument.id == KbChunk.document_id)
            .where(
                KbChunk.id.in_(chunk_ids),
                KbChunk.tenant_id == tenant_id,
                KbDocument.tenant_id == tenant_id,
                KbDocument.status == "ready",
            )
        )
        chunks_by_id = {chunk.id: (chunk, title) for chunk, title in result.all()}
        snippets: list[MemorySnippet] = []
        for hit in hits:
            row = chunks_by_id.get(hit.chunk_id)
            if row is None:
                continue
            chunk, title = row
            snippets.append(
                MemorySnippet(
                    id=str(chunk.id),
                    title=title,
                    text=chunk.text,
                    score=_bounded_score(hit.score),
                    source="qdrant",
                    tags={str(key): str(value) for key, value in (chunk.tags or {}).items()},
                )
            )
            if len(snippets) >= limit:
                break
        return snippets


async def get_memory_retriever(session: AsyncSession, tenant_id: UUID) -> MemoryRetriever:
    vector_store = get_vector_store()
    if vector_store is None:
        return DatabaseMemoryRetriever(session)
    ai_config = await session.get(TenantAIConfig, tenant_id)
    return VectorMemoryRetriever(
        session=session,
        vector_store=vector_store,
        embedder=get_embedder(ai_config.embedding_model if ai_config else None),
        fallback=DatabaseMemoryRetriever(session),
    )


def rank_snippets(
    snippets: Iterable[MemorySnippet],
    *,
    query: str,
    limit: int,
) -> list[MemorySnippet]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    scored: list[MemorySnippet] = []
    for snippet in snippets:
        text_tokens = tokenize(f"{snippet.title} {snippet.text} {' '.join(snippet.tags.values())}")
        overlap = sum((query_tokens & text_tokens).values())
        if overlap <= 0:
            continue
        score = min(1.0, overlap / max(1, sum(query_tokens.values())))
        scored.append(
            MemorySnippet(
                id=snippet.id,
                title=snippet.title,
                text=snippet.text,
                score=round(score, 3),
                source=snippet.source,
                tags=snippet.tags,
            )
        )

    return sorted(scored, key=lambda item: item.score, reverse=True)[:limit]


def _bounded_score(score: float) -> float:
    return round(max(0.0, min(1.0, score)), 3)


def default_sales_memory() -> list[MemorySnippet]:
    """Demo knowledge base for smoke tests and local development."""

    return [
        MemorySnippet(
            id="demo-price-iphone15",
            title="Прайс: iPhone 15 128ГБ",
            text=(
                "iPhone 15 128ГБ есть в наличии в цветах чёрный и синий. "
                "Цена — 79 990 ₽. По Казани доступна доставка в день заказа."
            ),
            score=1.0,
            tags={"topic": "price", "city": "Казань"},
        ),
        MemorySnippet(
            id="demo-delivery",
            title="Условия доставки",
            text=(
                "Доставка по Казани выполняется в день заказа, если заказ оформлен до 17:00. "
                "Самовывоз доступен ежедневно до 20:00."
            ),
            score=1.0,
            tags={"topic": "delivery"},
        ),
        MemorySnippet(
            id="demo-warranty",
            title="Гарантия",
            text=(
                "На новые устройства действует гарантия 12 месяцев. "
                "На восстановленные устройства действует гарантия 90 дней."
            ),
            score=1.0,
            tags={"topic": "warranty"},
        ),
        MemorySnippet(
            id="demo-installment",
            title="Рассрочка",
            text=(
                "Условия рассрочки зависят от текущей акции и банка-партнёра. "
                "Если клиент спрашивает про беспроцентную рассрочку, менеджер должен "
                "подтвердить актуальные условия."
            ),
            score=1.0,
            tags={"topic": "installment", "risk": "manager"},
        ),
    ]
