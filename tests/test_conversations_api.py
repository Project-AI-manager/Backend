"""Conversation actions API tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.core.security import create_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, Message
from app.models.email import EmailOutbox
from app.models.knowledge import KbCandidate
from app.models.tenant import Tenant
from app.models.user import User, UserNotificationSettings

TENANT_ID = uuid.UUID("55555555-5555-4555-8555-555555555501")
USER_ID = uuid.UUID("55555555-5555-4555-8555-555555555502")
CHANNEL_ID = uuid.UUID("55555555-5555-4555-8555-555555555503")
CUSTOMER_ID = uuid.UUID("55555555-5555-4555-8555-555555555504")
CONVERSATION_ID = uuid.UUID("55555555-5555-4555-8555-555555555505")


@pytest.fixture(autouse=True)
def disable_real_email_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EMAIL_SEND_ENABLED", False)


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
            User.__table__,
            UserNotificationSettings.__table__,
            EmailOutbox.__table__,
            Channel.__table__,
            Customer.__table__,
            Conversation.__table__,
            Message.__table__,
            KbCandidate.__table__,
        ):
            await conn.run_sync(create_table, table)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture()
def client(session_factory: async_sessionmaker[AsyncSession]) -> Generator[TestClient, None, None]:
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


async def seed_conversation(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Demo", slug="demo", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="owner@example.com",
                full_name="Owner",
                role="owner",
                password_hash=hash_password("demo-password"),
                status="active",
            )
        )
        session.add(
            Channel(
                id=CHANNEL_ID,
                tenant_id=TENANT_ID,
                type="telegram",
                name="Telegram",
                status="active",
                credentials_encrypted="fernet:test",
                settings={"webhook_path": "/api/v1/channels/webhook/telegram/test"},
            )
        )
        session.add(
            Customer(
                id=CUSTOMER_ID,
                tenant_id=TENANT_ID,
                display_name="Alina Petrova",
                note="",
            )
        )
        session.add(
            Conversation(
                id=CONVERSATION_ID,
                tenant_id=TENANT_ID,
                customer_id=CUSTOMER_ID,
                channel_id=CHANNEL_ID,
                status="escalated",
                assignee_user_id=None,
                last_message_at=datetime.now(UTC),
                last_message_preview="Сколько занимает подключение?",
                unread_count=3,
            )
        )
        session.add(
            Message(
                tenant_id=TENANT_ID,
                conversation_id=CONVERSATION_ID,
                direction="inbound",
                sender_type="customer",
                sender_user_id=None,
                text="Сколько занимает подключение?",
                attachments={},
                external_message_id="telegram:1",
                status="received",
                confidence=None,
                ai_meta={"source": "telegram", "chat_id": "7001"},
            )
        )
        await session.commit()


async def candidate_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(KbCandidate))
        return int(result.scalar_one())


async def escalation_outbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[EmailOutbox]:
    async with session_factory() as session:
        result = await session.execute(
            select(EmailOutbox).where(EmailOutbox.purpose == "escalation_alert")
        )
        return list(result.scalars().all())


def test_manager_reply_creates_message_and_kb_candidate(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply",
        headers=auth_headers(),
        json={"text": "Подключение Telegram обычно занимает около 15 минут."},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["delivered"] is False
    assert data["message"]["sender_type"] == "manager"
    assert data["message"]["sender_user_id"] == str(USER_ID)
    assert data["message"]["status"] == "pending"
    assert data["message"]["ai_meta"]["delivery"] == "delivery-disabled"
    assert data["conversation"]["status"] == "answered"
    assert data["conversation"]["unread_count"] == 0
    assert len(data["conversation"]["messages"]) == 2
    assert data["conversation"]["messages"][0]["sender_user_id"] is None
    assert data["conversation"]["messages"][1]["sender_user_id"] == str(USER_ID)
    assert asyncio.run(candidate_count(session_factory)) == 1


def test_conversation_responses_include_channel_type(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    list_response = client.get("/api/v1/conversations", headers=auth_headers())
    thread_response = client.get(
        f"/api/v1/conversations/{CONVERSATION_ID}",
        headers=auth_headers(),
    )

    assert list_response.status_code == 200, list_response.text
    assert list_response.json()[0]["channel_type"] == "telegram"
    assert thread_response.status_code == 200, thread_response.text
    assert thread_response.json()["channel_type"] == "telegram"
    assert thread_response.json()["messages"][0]["sender_user_id"] is None


def test_mark_conversation_read_clears_unread_count(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/read",
        headers=auth_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["unread_count"] == 0
    assert len(response.json()["messages"]) == 1

    list_response = client.get("/api/v1/conversations", headers=auth_headers())
    assert list_response.status_code == 200, list_response.text
    assert list_response.json()[0]["unread_count"] == 0


def test_mark_read_requires_conversation_in_current_tenant(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/read",
        headers=auth_headers(),
    )

    assert response.status_code == 404


def test_repeated_manual_escalation_does_not_send_duplicate_email(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/escalate",
        headers=auth_headers(),
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["conversation"]["status"] == "escalated"
    assert data["message"] is None
    assert data["delivered"] is None

    outbox = asyncio.run(escalation_outbox(session_factory))
    assert len(outbox) == 0


def test_manual_escalation_sends_email_with_latest_customer_message(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    async def reopen() -> None:
        async with session_factory() as session:
            conversation = await session.get(Conversation, CONVERSATION_ID)
            assert conversation is not None
            conversation.status = "answered"
            await session.commit()

    asyncio.run(reopen())
    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/escalate",
        headers=auth_headers(),
    )

    assert response.status_code == 200, response.text
    outbox = asyncio.run(escalation_outbox(session_factory))
    assert len(outbox) == 1
    assert outbox[0].to_email == "owner@example.com"
    assert outbox[0].metadata_json["conversation_id"] == str(CONVERSATION_ID)
    assert outbox[0].metadata_json["conversation_url"].endswith(
        f"/inbox?conversation={CONVERSATION_ID}"
    )
    assert "подключение" in outbox[0].body_text


def test_close_conversation_marks_it_closed(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/close",
        headers=auth_headers(),
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["conversation"]["status"] == "closed"
    assert data["conversation"]["unread_count"] == 0
    assert data["message"] is None
    assert data["delivered"] is None


def test_close_requires_conversation_in_current_tenant(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/close",
        headers=auth_headers(),
    )

    assert response.status_code == 404


def test_reply_requires_conversation_in_current_tenant(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_conversation(session_factory))

    response = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/reply",
        headers=auth_headers(),
        json={"text": "Ответ"},
    )

    assert response.status_code == 404
