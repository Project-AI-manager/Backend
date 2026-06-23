"""Индексация базы знаний: документ → чанки → эмбеддинги → Qdrant. См. knowledge-base."""


async def ingest_document(tenant_id: str, document_id: str) -> int:
    """Парсинг → чанкинг → эмбеддинги → upsert в Qdrant. Возвращает число чанков. TODO."""
    raise NotImplementedError
