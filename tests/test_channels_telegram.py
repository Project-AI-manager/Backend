"""Telegram channel API tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.security import create_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.knowledge import KbChunk, KbDocument
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User

TENANT_ID = uuid.UUID("44444444-4444-4444-8444-444444444401")
USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444402")


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
            TenantAIConfig.__table__,
            User.__table__,
            Channel.__table__,
            WebhookEvent.__table__,
            Customer.__table__,
            CustomerIdentity.__table__,
            Conversation.__table__,
            Message.__table__,
            KbDocument.__table__,
            KbChunk.__table__,
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


async def seed_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    auto_reply_enabled: bool = True,
) -> None:
    async with session_factory() as session:
        tenant = Tenant(id=TENANT_ID, name="ООО Север", slug="sever", status="active")
        session.add(tenant)
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
            TenantAIConfig(
                tenant_id=TENANT_ID,
                auto_reply_enabled=auto_reply_enabled,
                confidence_threshold=50,
                llm_provider="mock",
                embedding_model="local",
                system_prompt="Отвечай по базе знаний.",
            )
        )
        document = KbDocument(
            tenant_id=TENANT_ID,
            title="FAQ Telegram",
            source_type="manual",
            status="ready",
            version=1,
        )
        session.add(document)
        await session.flush()
        session.add(
            KbChunk(
                tenant_id=TENANT_ID,
                document_id=document.id,
                text="Подключение Telegram занимает 15 минут.",
                position=0,
                token_count=5,
                tags={"topic": "telegram"},
                version=1,
            )
        )
        await session.commit()


def telegram_payload(update_id: int = 1001, text: str = "Сколько занимает Telegram?") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 501,
            "date": 1_719_999_999,
            "chat": {"id": 7001, "type": "private"},
            "from": {
                "id": 9001,
                "is_bot": False,
                "first_name": "Алина",
                "last_name": "Петрова",
                "username": "alina",
            },
            "text": text,
        },
    }


async def count_rows(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


def test_connect_and_list_telegram_channel(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))

    created = client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={
            "type": "telegram",
            "bot_token": "1234567890:telegram-token",
            "bot_username": "demo_bot",
        },
    )

    assert created.status_code == 200, created.text
    data = created.json()
    assert data["type"] == "telegram"
    assert data["status"] == "active"
    assert data["settings"]["bot_username"] == "demo_bot"
    assert data["settings"]["webhook_path"].startswith("/api/v1/channels/webhook/telegram/")
    assert "bot_token" not in data["settings"]
    assert "webhook_secret" not in data["settings"]

    async def stored_credentials() -> str:
        async with session_factory() as session:
            result = await session.execute(select(Channel.credentials_encrypted))
            return str(result.scalar_one())

    encrypted_token = asyncio.run(stored_credentials())
    assert encrypted_token.startswith("fernet:")
    assert encrypted_token != "1234567890:telegram-token"

    listed = client.get("/api/v1/channels", headers=auth_headers())

    assert listed.status_code == 200
    assert listed.json()[0]["id"] == data["id"]


def test_telegram_webhook_creates_conversation_and_auto_reply(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    response = client.post("/api/v1/channels/webhook/telegram", json=telegram_payload())

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["duplicate"] is False
    assert data["decision"] == "auto_reply"
    assert data["conversation_id"]
    assert data["inbound_message_id"]
    assert data["outbound_message_id"]

    thread = client.get(f"/api/v1/conversations/{data['conversation_id']}", headers=auth_headers())

    assert thread.status_code == 200, thread.text
    thread_data = thread.json()
    assert thread_data["customer_name"] == "Алина Петрова"
    assert thread_data["status"] == "auto"
    assert len(thread_data["messages"]) == 2
    assert thread_data["messages"][0]["sender_type"] == "customer"
    assert thread_data["messages"][1]["sender_type"] == "ai"
    assert "15 минут" in thread_data["messages"][1]["text"]
    assert thread_data["messages"][1]["ai_meta"]["provider"] == "mock"


def test_telegram_webhook_is_idempotent(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    first = client.post("/api/v1/channels/webhook/telegram", json=telegram_payload())
    second = client.post("/api/v1/channels/webhook/telegram", json=telegram_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert asyncio.run(count_rows(session_factory, Message)) == 2
    assert asyncio.run(count_rows(session_factory, WebhookEvent)) == 1


def test_telegram_webhook_secret_selects_channel(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    second_tenant_id = uuid.UUID("44444444-4444-4444-8444-444444444411")
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))

    created = client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    webhook_path = created.json()["settings"]["webhook_path"]

    async def seed_second_channel() -> None:
        async with session_factory() as session:
            second_tenant = Tenant(
                id=second_tenant_id,
                name="ООО Юг",
                slug="yug",
                status="active",
            )
            session.add(second_tenant)
            session.add(
                TenantAIConfig(
                    tenant_id=second_tenant_id,
                    auto_reply_enabled=False,
                    confidence_threshold=50,
                    llm_provider="mock",
                    embedding_model="local",
                    system_prompt="",
                )
            )
            session.add(
                Channel(
                    tenant_id=second_tenant_id,
                    type="telegram",
                    name="Telegram",
                    status="active",
                    credentials_encrypted="second-token",
                    settings={
                        "webhook_path": "/api/v1/channels/webhook/telegram/second-secret",
                        "webhook_secret": "second-secret",
                    },
                )
            )
            await session.commit()

    asyncio.run(seed_second_channel())

    without_secret = client.post("/api/v1/channels/webhook/telegram", json=telegram_payload())
    response = client.post(webhook_path, json=telegram_payload(update_id=1003))

    assert without_secret.status_code == 400
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "auto_reply"


def test_telegram_webhook_escalates_without_auto_reply(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=False))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    response = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1002, text="Сколько занимает Telegram?"),
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["decision"] == "escalate"
    assert data["outbound_message_id"] is None

    conversations = client.get("/api/v1/conversations", headers=auth_headers())

    assert conversations.status_code == 200
    assert conversations.json()[0]["status"] == "escalated"
    assert asyncio.run(count_rows(session_factory, Message)) == 1
