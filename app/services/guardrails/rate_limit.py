"""Small process-local burst limiter plus database-backed tenant budgets.

The in-memory limiter protects the single-process MVP from bursts. The usage
ledger budget is the cross-process source of truth and remains effective when
the backend is later scaled to multiple workers.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ops import AIUsageEvent


class SlidingWindowLimiter:
    """Concurrency-safe fixed-duration limiter for burst protection."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str, *, limit: int, window_seconds: int = 60) -> bool:
        if limit <= 0:
            return False
        now = asyncio.get_running_loop().time()
        cutoff = now - window_seconds
        async with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True

    async def reset(self) -> None:
        """Clear state for deterministic tests and controlled restarts."""
        async with self._lock:
            self._events.clear()


burst_limiter = SlidingWindowLimiter()


async def tenant_llm_budget_reason(
    session: AsyncSession,
    tenant_id: UUID,
) -> str | None:
    """Return an explainable guard reason when a tenant budget is exhausted."""
    now = datetime.now(UTC)
    connection = await session.connection()
    has_usage_ledger = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).has_table("ai_usage_event")
    )
    if not has_usage_ledger:
        return None
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)

    calls_result = await session.execute(
        select(func.count(AIUsageEvent.id)).where(
            AIUsageEvent.tenant_id == tenant_id,
            AIUsageEvent.created_at >= hour_ago,
        )
    )
    if int(calls_result.scalar_one() or 0) >= settings.TENANT_LLM_CALLS_PER_HOUR:
        return "tenant_hourly_call_limit"

    totals_result = await session.execute(
        select(
            func.coalesce(func.sum(AIUsageEvent.total_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.client_charge_kopecks), 0),
        ).where(
            AIUsageEvent.tenant_id == tenant_id,
            AIUsageEvent.created_at >= day_ago,
        )
    )
    tokens, charge_kopecks = totals_result.one()
    if int(tokens or 0) >= settings.TENANT_LLM_TOKENS_PER_DAY:
        return "tenant_daily_token_limit"
    if int(charge_kopecks or 0) >= settings.TENANT_LLM_COST_KOPECKS_PER_DAY:
        return "tenant_daily_cost_limit"
    return None


async def acquire_tenant_llm_slot(session: AsyncSession, tenant_id: UUID) -> str | None:
    """Serialize the budget check per tenant when PostgreSQL is available.

    The advisory transaction lock closes the race where multiple workers could
    all pass the same hard-cap check simultaneously. SQLite tests/local mode
    stay single-process and rely on the process-local burst limiter.
    """
    connection = await session.connection()
    if connection.dialect.name == "postgresql":
        lock_key = tenant_id.int % (2**63 - 1)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    return await tenant_llm_budget_reason(session, tenant_id)
