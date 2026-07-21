"""Small producer-side contract for ARQ jobs.

Keeping this module independent from Redis construction makes API handlers and
tests able to enqueue work through any object implementing ``enqueue_job``.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID


class JobQueue(Protocol):
    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> object | None: ...


async def enqueue_inbound_message(queue: JobQueue, message_id: UUID) -> bool:
    """Enqueue a message once while an existing ARQ job id is still retained."""
    job = await queue.enqueue_job(
        "process_inbound_message",
        str(message_id),
        _job_id=f"inbound:{message_id}",
    )
    return job is not None


async def enqueue_document_reindex(queue: JobQueue, document_id: UUID) -> bool:
    """Enqueue a document reindex with a deterministic deduplication key."""
    job = await queue.enqueue_job(
        "reindex_document",
        str(document_id),
        _job_id=f"reindex:{document_id}",
    )
    return job is not None
