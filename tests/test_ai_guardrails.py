"""Focused tests for cost and burst guardrails."""

import uuid

import pytest

from app.services.guardrails.rate_limit import SlidingWindowLimiter


@pytest.mark.asyncio
async def test_sliding_window_limiter_is_keyed_and_rejects_before_next_action() -> None:
    limiter = SlidingWindowLimiter()

    assert await limiter.allow("tenant-a:user-a:ip-a", limit=2)
    assert await limiter.allow("tenant-a:user-a:ip-a", limit=2)
    assert not await limiter.allow("tenant-a:user-a:ip-a", limit=2)
    assert await limiter.allow("tenant-b:user-a:ip-a", limit=2)


@pytest.mark.asyncio
async def test_tenant_budget_reports_hourly_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.core.config import settings
    from app.db.base import Base
    from app.models.ops import AIUsageEvent
    from app.models.tenant import Tenant
    from app.services.guardrails.rate_limit import tenant_llm_budget_reason

    tenant_id = uuid.uuid4()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "TENANT_LLM_CALLS_PER_HOUR", 1)
    async with factory() as session:
        session.add(Tenant(id=tenant_id, name="Тест", slug="test", status="active"))
        session.add(
            AIUsageEvent(
                tenant_id=tenant_id,
                provider="mock",
                outcome="error",
                error_code="timeout",
            )
        )
        await session.commit()

        assert await tenant_llm_budget_reason(session, tenant_id) == "tenant_hourly_call_limit"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_limit", "cost_limit", "expected"),
    [
        (10, 10_000, "tenant_daily_token_limit"),
        (10_000, 5, "tenant_daily_cost_limit"),
    ],
)
async def test_tenant_budget_enforces_daily_hard_caps(
    monkeypatch: pytest.MonkeyPatch,
    token_limit: int,
    cost_limit: int,
    expected: str,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.core.config import settings
    from app.db.base import Base
    from app.models.ops import AIUsageEvent
    from app.models.tenant import Tenant
    from app.services.guardrails.rate_limit import tenant_llm_budget_reason

    tenant_id = uuid.uuid4()
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "TENANT_LLM_CALLS_PER_HOUR", 100)
    monkeypatch.setattr(settings, "TENANT_LLM_TOKENS_PER_DAY", token_limit)
    monkeypatch.setattr(settings, "TENANT_LLM_COST_KOPECKS_PER_DAY", cost_limit)
    async with factory() as session:
        session.add(Tenant(id=tenant_id, name="Тест", slug="test", status="active"))
        session.add(
            AIUsageEvent(
                tenant_id=tenant_id,
                provider="mock",
                outcome="completed",
                total_tokens=10,
                client_charge_kopecks=5,
            )
        )
        await session.commit()

        assert await tenant_llm_budget_reason(session, tenant_id) == expected
    await engine.dispose()
