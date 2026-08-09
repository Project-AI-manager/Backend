"""Regression coverage for the first billable LLM call in a month."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, Message
from app.models.ops import AIDecisionEvent, AIUsageEvent, UsageCounter
from app.models.tenant import Tenant, TenantAIConfig
from app.services.billing.ledger import record_llm_attempt
from app.services.billing.usage import calculate_usage_cost
from app.services.channels.telegram import process_channel_inbound_message
from app.services.guardrails.rate_limit import burst_limiter
from app.services.ml.contracts import MLAnswerResult, PromptBundle
from app.services.rag.llm import LLMUsage

TENANT_ID = uuid.UUID("12121212-1212-4212-8212-121212121201")
CHANNEL_ID = uuid.UUID("12121212-1212-4212-8212-121212121202")
CUSTOMER_ID = uuid.UUID("12121212-1212-4212-8212-121212121203")
CONVERSATION_ID = uuid.UUID("12121212-1212-4212-8212-121212121204")
INBOUND_ID = uuid.UUID("12121212-1212-4212-8212-121212121205")
MODEL = "gpt-5.6-sol"
USAGE = LLMUsage(input_tokens=1_000, output_tokens=100, total_tokens=1_100)


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        for table in (
            Tenant.__table__,
            TenantAIConfig.__table__,
            Channel.__table__,
            Customer.__table__,
            Conversation.__table__,
            Message.__table__,
            WebhookEvent.__table__,
            AIUsageEvent.__table__,
            UsageCounter.__table__,
            AIDecisionEvent.__table__,
        ):
            await connection.run_sync(create_table, table)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_first_completed_llm_event_creates_monthly_counter_and_expense(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def exercise() -> tuple[AIUsageEvent, UsageCounter]:
        async with session_factory() as session:
            session.add(Tenant(id=TENANT_ID, name="First usage", slug="first-usage"))
            await session.commit()

            event = await record_llm_attempt(
                session,
                tenant_id=TENANT_ID,
                provider="openai-compatible",
                model=MODEL,
                usage=USAGE,
                outcome="completed",
            )
            await session.commit()
            await session.refresh(event)
            counter = (
                await session.execute(
                    select(UsageCounter).where(UsageCounter.tenant_id == TENANT_ID)
                )
            ).scalar_one()
            return event, counter

    event, counter = asyncio.run(exercise())
    expected_cost = calculate_usage_cost(MODEL, USAGE)

    assert event.outcome == "completed"
    assert event.client_charge_kopecks == expected_cost.client_charge_kopecks
    assert counter.period == datetime.now(UTC).strftime("%Y-%m")
    assert counter.ai_replies_count == 1
    assert counter.expenses_kopecks == expected_cost.client_charge_kopecks


def test_first_telegram_auto_reply_persists_with_usage_accounting(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.channels import telegram

    async def fake_memory_retriever(_session: AsyncSession, _tenant_id: uuid.UUID) -> object:
        return object()

    async def fake_answer(_self: object, _request: object) -> MLAnswerResult:
        return MLAnswerResult(
            answer="Ответ из базы знаний.",
            confidence=0.95,
            decision="auto_reply",
            decision_reason="auto_reply_grounded",
            sources=(),
            provider="openai-compatible",
            model=MODEL,
            request_id="request-first-monthly-usage",
            usage=USAGE,
            prompt=PromptBundle(system_prompt="", user_prompt="", context_block=""),
        )

    async def fake_send(_channel: Channel, chat_id: str, text: str) -> bool:
        assert chat_id == "7001"
        assert text == "Ответ из базы знаний."
        return True

    monkeypatch.setattr(telegram, "get_memory_retriever", fake_memory_retriever)
    monkeypatch.setattr(telegram, "get_llm", lambda _provider: object())
    monkeypatch.setattr(telegram.MLMessageService, "answer", fake_answer)
    monkeypatch.setattr(telegram, "send_telegram_message", fake_send)

    async def exercise() -> None:
        await burst_limiter.reset()
        async with session_factory() as session:
            session.add(Tenant(id=TENANT_ID, name="Telegram usage", slug="telegram-usage"))
            session.add(
                TenantAIConfig(
                    tenant_id=TENANT_ID,
                    auto_reply_enabled=True,
                    confidence_threshold=100,
                    llm_provider="openai-compatible",
                    embedding_model="local",
                    system_prompt="",
                )
            )
            session.add(
                Channel(
                    id=CHANNEL_ID,
                    tenant_id=TENANT_ID,
                    type="telegram",
                    name="Telegram",
                    status="active",
                    credentials_encrypted="encrypted-token",
                    settings={},
                )
            )
            session.add(Customer(id=CUSTOMER_ID, tenant_id=TENANT_ID, display_name="Клиент"))
            session.add(
                Conversation(
                    id=CONVERSATION_ID,
                    tenant_id=TENANT_ID,
                    customer_id=CUSTOMER_ID,
                    channel_id=CHANNEL_ID,
                    status="open",
                    last_message_at=datetime.now(UTC),
                    last_message_preview="Вопрос",
                )
            )
            session.add(
                Message(
                    id=INBOUND_ID,
                    tenant_id=TENANT_ID,
                    conversation_id=CONVERSATION_ID,
                    direction="inbound",
                    sender_type="customer",
                    text="Вопрос",
                    status="received",
                    external_message_id="telegram:first:1",
                    ai_meta={"source": "telegram", "chat_id": "7001"},
                )
            )
            await session.commit()

            result = await process_channel_inbound_message(session, INBOUND_ID)
            assert result.decision == "auto_reply"

    asyncio.run(exercise())

    async def stored_state() -> tuple[Message, AIUsageEvent, UsageCounter, int]:
        async with session_factory() as session:
            outbound = (
                await session.execute(
                    select(Message).where(Message.direction == "outbound")
                )
            ).scalar_one()
            event = (await session.execute(select(AIUsageEvent))).scalar_one()
            counter = (await session.execute(select(UsageCounter))).scalar_one()
            decision_count = int(
                (
                    await session.execute(select(func.count()).select_from(AIDecisionEvent))
                ).scalar_one()
            )
            return outbound, event, counter, decision_count

    outbound, event, counter, decision_count = asyncio.run(stored_state())
    expected_cost = calculate_usage_cost(MODEL, USAGE)

    assert outbound.text == "Ответ из базы знаний."
    assert outbound.status == "sent"
    assert event.outcome == "completed"
    assert event.message_id == outbound.id
    assert counter.ai_replies_count == 1
    assert counter.expenses_kopecks == expected_cost.client_charge_kopecks
    assert decision_count == 1


def test_committed_telegram_auto_reply_retries_delivery_without_rerunning_llm(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.channels import telegram

    llm_calls = 0
    delivery_calls = 0

    async def fake_memory_retriever(_session: AsyncSession, _tenant_id: uuid.UUID) -> object:
        return object()

    async def fake_answer(_self: object, _request: object) -> MLAnswerResult:
        nonlocal llm_calls
        llm_calls += 1
        return MLAnswerResult(
            answer="Надёжный ответ.",
            confidence=0.95,
            decision="auto_reply",
            decision_reason="auto_reply_grounded",
            sources=(),
            provider="openai-compatible",
            model=MODEL,
            request_id="request-delivery-retry",
            usage=USAGE,
            prompt=PromptBundle(system_prompt="", user_prompt="", context_block=""),
        )

    async def fail_once_then_deliver(_channel: Channel, _chat_id: str, _text: str) -> bool:
        nonlocal delivery_calls
        delivery_calls += 1
        if delivery_calls == 1:
            raise RuntimeError("provider unavailable after durable checkpoint")
        return True

    monkeypatch.setattr(telegram, "get_memory_retriever", fake_memory_retriever)
    monkeypatch.setattr(telegram, "get_llm", lambda _provider: object())
    monkeypatch.setattr(telegram.MLMessageService, "answer", fake_answer)
    monkeypatch.setattr(telegram, "send_telegram_message", fail_once_then_deliver)

    async def seed_and_process() -> None:
        await burst_limiter.reset()
        async with session_factory() as session:
            session.add(Tenant(id=TENANT_ID, name="Retry delivery", slug="retry-delivery"))
            session.add(
                TenantAIConfig(
                    tenant_id=TENANT_ID,
                    auto_reply_enabled=True,
                    confidence_threshold=100,
                    llm_provider="openai-compatible",
                    embedding_model="local",
                    system_prompt="",
                )
            )
            session.add(
                Channel(
                    id=CHANNEL_ID,
                    tenant_id=TENANT_ID,
                    type="telegram",
                    name="Telegram",
                    status="active",
                    credentials_encrypted="encrypted-token",
                    settings={},
                )
            )
            session.add(Customer(id=CUSTOMER_ID, tenant_id=TENANT_ID, display_name="Клиент"))
            session.add(
                Conversation(
                    id=CONVERSATION_ID,
                    tenant_id=TENANT_ID,
                    customer_id=CUSTOMER_ID,
                    channel_id=CHANNEL_ID,
                    status="open",
                    last_message_at=datetime.now(UTC),
                    last_message_preview="Вопрос",
                )
            )
            session.add(
                Message(
                    id=INBOUND_ID,
                    tenant_id=TENANT_ID,
                    conversation_id=CONVERSATION_ID,
                    direction="inbound",
                    sender_type="customer",
                    text="Вопрос",
                    status="received",
                    external_message_id="telegram:retry:1",
                    ai_meta={"source": "telegram", "chat_id": "7001"},
                )
            )
            await session.commit()

            with pytest.raises(RuntimeError, match="provider unavailable"):
                await process_channel_inbound_message(session, INBOUND_ID)

        async with session_factory() as session:
            durable_outbound = (
                await session.execute(select(Message).where(Message.direction == "outbound"))
            ).scalar_one()
            assert durable_outbound.status == "pending"
            assert (
                await session.execute(select(func.count()).select_from(AIUsageEvent))
            ).scalar_one() == 1
            assert (
                await session.execute(select(func.count()).select_from(AIDecisionEvent))
            ).scalar_one() == 1

            result = await process_channel_inbound_message(session, INBOUND_ID)
            assert result.decision == "auto_reply"

    asyncio.run(seed_and_process())

    assert llm_calls == 1
    assert delivery_calls == 2

    async def stored_state() -> tuple[Message, int, int]:
        async with session_factory() as session:
            outbound = (
                await session.execute(select(Message).where(Message.direction == "outbound"))
            ).scalar_one()
            usage_count = int(
                (await session.execute(select(func.count()).select_from(AIUsageEvent))).scalar_one()
            )
            decision_count = int(
                (
                    await session.execute(select(func.count()).select_from(AIDecisionEvent))
                ).scalar_one()
            )
            return outbound, usage_count, decision_count

    outbound, usage_count, decision_count = asyncio.run(stored_state())
    assert outbound.status == "sent"
    assert usage_count == 1
    assert decision_count == 1
