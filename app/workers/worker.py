"""Фоновые задачи ARQ: обработка входящих, индексация базы знаний, follow-up.

Запуск: uv run arq app.workers.worker.WorkerSettings
"""
from arq.connections import RedisSettings

from app.core.config import settings
from app.services.knowledge import ingest_document


async def process_inbound_message(ctx: dict, message_id: str) -> None:
    """Долгая обработка обращения: RAG → уверенность → авто-ответ или эскалация. TODO."""
    # from app.services.rag.pipeline import answer ...


async def reindex_document(ctx: dict, tenant_id: str, document_id: str) -> int:
    """Replace one document's vectors using the current embedding configuration."""
    return await ingest_document(tenant_id, document_id)


class WorkerSettings:
    functions = [process_inbound_message, reindex_document]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
