"""Background ARQ jobs for inbound messages and knowledge indexing.

Run: ``uv run arq app.workers.worker.WorkerSettings``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.channels.telegram import process_telegram_inbound_message
from app.services.knowledge import reindex_kb_document


async def process_inbound_message(ctx: dict[str, Any], message_id: str) -> dict[str, Any]:
    """Process a persisted Telegram inbound through RAG and decisioning."""
    parsed_message_id = _parse_uuid(message_id, "message_id")
    async with _session_factory(ctx)() as session:
        result = await process_telegram_inbound_message(session, parsed_message_id)
        return result.model_dump(mode="json")


async def reindex_document(
    ctx: dict[str, Any],
    document_id: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Replace a document's Qdrant points; safe for ARQ retries."""
    parsed_document_id = _parse_uuid(document_id, "document_id")
    async with _session_factory(ctx)() as session:
        chunks_count = await reindex_kb_document(session, parsed_document_id)
        return {
            "document_id": str(parsed_document_id),
            "status": "missing" if chunks_count is None else "indexed",
            "chunks_count": chunks_count or 0,
        }


def _session_factory(ctx: dict[str, Any]) -> Callable[..., Any]:
    factory = ctx.get("session_factory", SessionLocal)
    if not isinstance(factory, async_sessionmaker) and not callable(factory):
        raise TypeError("ARQ context session_factory must be an async_sessionmaker")
    return factory


def _parse_uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}") from exc


class WorkerSettings:
    functions = [process_inbound_message, reindex_document]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_tries = 4
    job_timeout = 120
    keep_result = 3600
