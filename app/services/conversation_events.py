"""Tenant-scoped Server-Sent Events for inbox changes.

The database remains the durable source of truth. The stream only tells a
client that its cached list/thread should be refreshed, so dropped events are
safe and reconnecting clients always converge on current state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.conversation import Conversation, Message


async def conversation_event_signature(
    session: AsyncSession,
    tenant_id: UUID,
) -> tuple[object, ...]:
    """Return a cheap tenant-only watermark that changes for any inbox mutation."""
    conversation_stats = (
        await session.execute(
            select(
                func.count(Conversation.id),
                func.max(Conversation.updated_at),
                func.coalesce(func.sum(Conversation.unread_count), 0),
            ).where(Conversation.tenant_id == tenant_id)
        )
    ).one()
    message_stats = (
        await session.execute(
            select(func.count(Message.id), func.max(Message.updated_at)).where(
                Message.tenant_id == tenant_id
            )
        )
    ).one()
    return (*conversation_stats, *message_stats)


async def conversation_event_stream(
    tenant_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
) -> AsyncIterator[str]:
    """Yield an SSE invalidation stream scoped to exactly one authenticated tenant."""
    previous: tuple[object, ...] | None = None
    sequence = 0
    last_keepalive = asyncio.get_running_loop().time()
    interval = max(0.25, settings.CONVERSATION_SSE_POLL_INTERVAL_SEC)

    yield "retry: 3000\nevent: ready\ndata: {}\n\n"
    try:
        while True:
            try:
                async with session_factory() as session:
                    current = await conversation_event_signature(session, tenant_id)
                if previous is not None and current != previous:
                    sequence += 1
                    payload = json.dumps({"sequence": sequence}, separators=(",", ":"))
                    yield f"id: {sequence}\nevent: conversations.changed\ndata: {payload}\n\n"
                previous = current
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - client reconnect/REST fallback handles this.
                yield "event: transport.error\ndata: {}\n\n"

            now = asyncio.get_running_loop().time()
            if now - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = now
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return
