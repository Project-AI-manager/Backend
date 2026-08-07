"""Durable accounting for every provider generation attempt."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ops import AIUsageEvent, UsageCounter
from app.services.billing.usage import calculate_usage_cost, reasoning_effort
from app.services.rag.llm import LLMUsage


async def record_llm_attempt(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    provider: str,
    outcome: str,
    model: str = "",
    usage: LLMUsage | None = None,
    request_id: str = "",
    error_code: str = "",
    customer_id: UUID | None = None,
    conversation_id: UUID | None = None,
    message_id: UUID | None = None,
    metadata: dict | None = None,
) -> AIUsageEvent:
    """Append an immutable event and update the monthly materialized counter."""
    connection = await session.connection()
    has_usage_ledger = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).has_table("ai_usage_event")
    )
    if not has_usage_ledger:
        # Transitional compatibility while an old test/deployment schema is
        # rolling forward. Production startup applies the migration first.
        return AIUsageEvent(tenant_id=tenant_id, provider=provider, outcome=outcome)

    measured_usage = usage or LLMUsage()
    cost = calculate_usage_cost(model, measured_usage)
    event = AIUsageEvent(
        tenant_id=tenant_id,
        customer_id=customer_id,
        conversation_id=conversation_id,
        message_id=message_id,
        provider=provider,
        model=model,
        request_id=request_id,
        reasoning_effort=reasoning_effort(model),
        input_tokens=measured_usage.input_tokens,
        cached_input_tokens=measured_usage.cached_input_tokens,
        cache_write_tokens=measured_usage.cache_write_tokens,
        output_tokens=measured_usage.output_tokens,
        reasoning_tokens=measured_usage.reasoning_tokens,
        total_tokens=measured_usage.total_tokens,
        provider_cost_microrubles=cost.provider_cost_microrubles,
        client_charge_kopecks=cost.client_charge_kopecks,
        outcome=outcome,
        error_code=error_code,
        metadata_json=metadata or {},
    )
    session.add(event)

    has_usage_counter = await connection.run_sync(
        lambda sync_connection: inspect(sync_connection).has_table("usage_counter")
    )
    if not has_usage_counter:
        await session.flush()
        return event

    period = datetime.now(UTC).strftime("%Y-%m")
    result = await session.execute(
        select(UsageCounter)
        .where(UsageCounter.tenant_id == tenant_id, UsageCounter.period == period)
        .with_for_update()
    )
    counter = result.scalar_one_or_none()
    if counter is None:
        counter = UsageCounter(tenant_id=tenant_id, period=period)
        session.add(counter)
    if outcome == "completed":
        counter.ai_replies_count += 1
    counter.expenses_kopecks += cost.client_charge_kopecks
    await session.flush()
    return event
