"""Analytics API tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.security import create_token
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, Message
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import Plan, Subscription, UsageCounter
from app.models.tenant import Tenant

TENANT_ID = uuid.UUID("66666666-6666-4666-8666-666666666601")
OTHER_TENANT_ID = uuid.UUID("66666666-6666-4666-8666-666666666602")
USER_ID = uuid.UUID("66666666-6666-4666-8666-666666666603")
CHANNEL_ID = uuid.UUID("66666666-6666-4666-8666-666666666604")
CUSTOMER_ID = uuid.UUID("66666666-6666-4666-8666-666666666605")


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for table in (
            Tenant.__table__,
            Channel.__table__,
            Customer.__table__,
            Conversation.__table__,
            Message.__table__,
            Plan.__table__,
            Subscription.__table__,
            UsageCounter.__table__,
            KbDocument.__table__,
            KbChunk.__table__,
            KbCandidate.__table__,
        ):
            await conn.run_sync(create_table, table)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture()
def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> Generator[TestClient, None, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def auth_headers() -> dict[str, str]:
    token = create_token(USER_ID, tenant_id=TENANT_ID, role="owner")
    return {"Authorization": f"Bearer {token}"}


async def seed_analytics_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        now = datetime.now(UTC)
        session.add_all(
            [
                Tenant(id=TENANT_ID, name="Demo", slug="demo", status="active"),
                Tenant(id=OTHER_TENANT_ID, name="Other", slug="other", status="active"),
                Channel(
                    id=CHANNEL_ID,
                    tenant_id=TENANT_ID,
                    type="telegram",
                    name="Telegram",
                    status="active",
                    credentials_encrypted="fernet:test",
                    settings={},
                ),
                Customer(
                    id=CUSTOMER_ID,
                    tenant_id=TENANT_ID,
                    display_name="Alina",
                    note="",
                ),
            ]
        )
        plan = Plan(
            code="demo",
            name="Demo",
            price_month=0,
            dialog_limit=500,
            channel_limit=1,
            features={},
        )
        session.add(plan)
        await session.flush()
        session.add(Subscription(tenant_id=TENANT_ID, plan_id=plan.id, status="trial"))
        session.add(
            UsageCounter(
                tenant_id=TENANT_ID,
                period=now.strftime("%Y-%m"),
                dialogs_count=9,
                ai_replies_count=2,
            )
        )

        open_conversation = Conversation(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            channel_id=CHANNEL_ID,
            status="open",
            last_message_at=now,
            last_message_preview="Open",
            unread_count=1,
        )
        auto_conversation = Conversation(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            channel_id=CHANNEL_ID,
            status="auto",
            last_message_at=now,
            last_message_preview="Auto",
            unread_count=0,
        )
        escalated_conversation = Conversation(
            tenant_id=TENANT_ID,
            customer_id=CUSTOMER_ID,
            channel_id=CHANNEL_ID,
            status="escalated",
            last_message_at=now,
            last_message_preview="Escalated",
            unread_count=2,
        )
        other_conversation = Conversation(
            tenant_id=OTHER_TENANT_ID,
            customer_id=CUSTOMER_ID,
            channel_id=CHANNEL_ID,
            status="auto",
            last_message_at=now,
            last_message_preview="Other",
            unread_count=0,
        )
        session.add_all(
            [
                open_conversation,
                auto_conversation,
                escalated_conversation,
                other_conversation,
            ]
        )
        await session.flush()

        session.add_all(
            [
                Message(
                    tenant_id=TENANT_ID,
                    conversation_id=auto_conversation.id,
                    direction="inbound",
                    sender_type="customer",
                    text="Question 1",
                    attachments={},
                    status="received",
                    confidence=None,
                    ai_meta={},
                    created_at=now - timedelta(seconds=70),
                ),
                Message(
                    tenant_id=TENANT_ID,
                    conversation_id=auto_conversation.id,
                    direction="outbound",
                    sender_type="ai",
                    text="Answer 1",
                    attachments={},
                    status="sent",
                    confidence=0.8,
                    ai_meta={"provider": "mock"},
                    created_at=now - timedelta(seconds=10),
                ),
                Message(
                    tenant_id=TENANT_ID,
                    conversation_id=escalated_conversation.id,
                    direction="inbound",
                    sender_type="customer",
                    text="Question 2",
                    attachments={},
                    status="received",
                    confidence=None,
                    ai_meta={},
                    created_at=now - timedelta(seconds=120),
                ),
                Message(
                    tenant_id=TENANT_ID,
                    conversation_id=escalated_conversation.id,
                    direction="outbound",
                    sender_type="manager",
                    text="Answer 2",
                    attachments={},
                    status="sent",
                    confidence=None,
                    ai_meta={},
                    created_at=now - timedelta(seconds=30),
                ),
                Message(
                    tenant_id=OTHER_TENANT_ID,
                    conversation_id=other_conversation.id,
                    direction="outbound",
                    sender_type="ai",
                    text="Other answer",
                    attachments={},
                    status="sent",
                    confidence=1.0,
                    ai_meta={},
                    created_at=now,
                ),
            ]
        )

        ready_document = KbDocument(
            tenant_id=TENANT_ID,
            title="FAQ",
            source_type="manual",
            status="ready",
            version=1,
        )
        archived_document = KbDocument(
            tenant_id=TENANT_ID,
            title="Old",
            source_type="manual",
            status="archived",
            version=1,
        )
        session.add_all([ready_document, archived_document])
        await session.flush()
        session.add_all(
            [
                KbChunk(
                    tenant_id=TENANT_ID,
                    document_id=ready_document.id,
                    text="Chunk 1",
                    position=0,
                    token_count=2,
                    tags={},
                    version=1,
                ),
                KbChunk(
                    tenant_id=TENANT_ID,
                    document_id=ready_document.id,
                    text="Chunk 2",
                    position=1,
                    token_count=2,
                    tags={},
                    version=1,
                ),
                KbCandidate(
                    tenant_id=TENANT_ID,
                    conversation_id=escalated_conversation.id,
                    question="Q",
                    answer="A",
                    suggested_by="manager",
                    status="pending",
                ),
            ]
        )

        await session.commit()


def test_analytics_overview_returns_tenant_kpis(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_analytics_data(session_factory))

    response = client.get("/api/v1/analytics/overview", headers=auth_headers())

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dialogs_total"] == 3
    assert data["dialogs_open"] == 1
    assert data["dialogs_auto"] == 1
    assert data["dialogs_escalated"] == 1
    assert data["auto_reply_rate"] == 0.3333
    assert data["escalation_rate"] == 0.3333
    assert data["avg_response_sec"] == 75
    assert data["avg_ai_confidence"] == 0.8
    assert data["ai_replies_count"] == 1
    assert data["manager_replies_count"] == 1
    assert data["inbound_messages_count"] == 2
    assert data["dialogs_used"] == 9
    assert data["dialogs_limit"] == 500
    assert data["knowledge_documents_ready"] == 1
    assert data["knowledge_chunks_count"] == 2
    assert data["pending_candidates_count"] == 1
    assert data["status_breakdown"] == [
        {"status": "auto", "count": 1},
        {"status": "escalated", "count": 1},
        {"status": "open", "count": 1},
    ]


def test_analytics_overview_returns_zeroes_for_empty_tenant(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def seed_empty_tenant() -> None:
        async with session_factory() as session:
            session.add(Tenant(id=TENANT_ID, name="Empty", slug="empty", status="active"))
            await session.commit()

    asyncio.run(seed_empty_tenant())

    response = client.get("/api/v1/analytics/overview", headers=auth_headers())

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dialogs_total"] == 0
    assert data["auto_reply_rate"] == 0
    assert data["avg_response_sec"] == 0
    assert data["status_breakdown"] == []
