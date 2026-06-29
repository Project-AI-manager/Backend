"""Эмбеддинги за интерфейсом. Старт — локальная модель (multilingual-e5), без внешних доступов."""
from abc import ABC, abstractmethod

VECTOR_DIM = 1024  # multilingual-e5-large


class EmbeddingProvider(ABC):
    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbedding(EmbeddingProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        # TODO: sentence-transformers multilingual-e5-large
        return [[0.0] * VECTOR_DIM for _ in texts]


def get_embedder() -> EmbeddingProvider:
    return LocalEmbedding()  # TODO: ветка под облачные эмбеддинги по settings.EMBEDDING_PROVIDER
