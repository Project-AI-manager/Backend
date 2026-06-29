"""Company memory retrieval for the first ML module iteration.

Today this is a deterministic keyword retriever so the whole ML flow can be
tested without Qdrant, Postgres or external API keys. Later we can replace the
retriever implementation with Qdrant-backed vector search while keeping the
orchestrator and API contract stable.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import Counter

from app.services.ml.contracts import MemorySnippet

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
        tenant_id: str,
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
    """Small local REC/RAG-like memory used before Qdrant is connected."""

    def __init__(self, snippets: list[MemorySnippet] | None = None) -> None:
        self.snippets = snippets or default_sales_memory()

    async def retrieve(self, *, tenant_id: str, query: str, limit: int = 4) -> list[MemorySnippet]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scored: list[MemorySnippet] = []
        for snippet in self.snippets:
            text_tokens = tokenize(
                f"{snippet.title} {snippet.text} {' '.join(snippet.tags.values())}"
            )
            overlap = sum((query_tokens & text_tokens).values())
            if overlap <= 0:
                continue
            score = min(1.0, overlap / max(3, len(query_tokens)))
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
