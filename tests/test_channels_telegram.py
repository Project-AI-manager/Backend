"""Telegram channel API tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace
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
from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.email import EmailOutbox
from app.models.knowledge import KbChunk, KbDocument
from app.models.ops import AIDecisionEvent, AIUsageEvent
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User, UserNotificationSettings
from app.services.channels.telegram import process_telegram_inbound_message
from app.services.ml.service import MLMessageService

TENANT_ID = uuid.UUID("44444444-4444-4444-8444-444444444401")
USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444402")


@pytest.fixture(autouse=True)
def disable_real_email_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "EMAIL_SEND_ENABLED", False)
    # Unit tests exercise the SQL retrieval fallback. A running local backend may
    # legitimately hold the embedded Qdrant lock, so tests must not share it.
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)


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
            UserNotificationSettings.__table__,
            EmailOutbox.__table__,
            Channel.__table__,
            WebhookEvent.__table__,
            Customer.__table__,
            CustomerIdentity.__table__,
            Conversation.__table__,
            Message.__table__,
            AIUsageEvent.__table__,
            AIDecisionEvent.__table__,
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


def auth_headers(role: str = "owner") -> dict[str, str]:
    token = create_token(USER_ID, tenant_id=TENANT_ID, role=role)
    return {"Authorization": f"Bearer {token}"}


async def seed_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    auto_reply_enabled: bool = True,
    confidence_threshold: int = 50,
    role: str = "owner",
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
                role=role,
                password_hash=hash_password("demo-password"),
                status="active",
            )
        )
        session.add(
            TenantAIConfig(
                tenant_id=TENANT_ID,
                auto_reply_enabled=auto_reply_enabled,
                confidence_threshold=confidence_threshold,
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


async def escalation_outbox(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[EmailOutbox]:
    async with session_factory() as session:
        result = await session.execute(
            select(EmailOutbox).where(EmailOutbox.purpose == "escalation_alert")
        )
        return list(result.scalars().all())


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


def test_disconnect_telegram_channel_clears_credentials_and_preserves_history(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    channel_id = uuid.UUID(created.json()["id"])

    async def seed_history() -> uuid.UUID:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Client")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=channel_id,
                status="closed",
            )
            session.add(conversation)
            await session.commit()
            return conversation.id

    conversation_id = asyncio.run(seed_history())
    response = client.delete(
        f"/api/v1/channels/{channel_id}",
        headers=auth_headers(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disabled"
    assert response.json()["settings"] == {}

    async def stored_state() -> tuple[Channel | None, Conversation | None]:
        async with session_factory() as session:
            return (
                await session.get(Channel, channel_id),
                await session.get(Conversation, conversation_id),
            )

    channel, conversation = asyncio.run(stored_state())
    assert channel is not None
    assert channel.credentials_encrypted == ""
    assert channel.settings == {}
    assert conversation is not None


def test_disconnect_channel_requires_admin_and_hides_unknown_id(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    channel_id = created.json()["id"]

    async def make_manager() -> None:
        async with session_factory() as session:
            user = await session.get(User, USER_ID)
            assert user is not None
            user.role = "manager"
            await session.commit()

    asyncio.run(make_manager())

    forbidden = client.delete(
        f"/api/v1/channels/{channel_id}",
        headers=auth_headers(role="manager"),
    )

    async def restore_owner() -> None:
        async with session_factory() as session:
            user = await session.get(User, USER_ID)
            assert user is not None
            user.role = "owner"
            await session.commit()

    asyncio.run(restore_owner())
    missing = client.delete(
        f"/api/v1/channels/{uuid.uuid4()}",
        headers=auth_headers(),
    )

    assert forbidden.status_code == 403
    assert missing.status_code == 404


def test_manager_cannot_connect_telegram_channel(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, role="manager"))

    listed = client.get("/api/v1/channels", headers=auth_headers(role="manager"))
    response = client.post(
        "/api/v1/channels",
        headers=auth_headers(role="manager"),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    assert listed.status_code == 403
    assert response.status_code == 403


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

    async def usage_events_count() -> int:
        async with session_factory() as session:
            result = await session.execute(select(func.count()).select_from(AIUsageEvent))
            return result.scalar_one()

    assert asyncio.run(usage_events_count()) == 1


def test_new_inbound_reopens_closed_conversation_without_duplicate(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    class EmptyRetriever:
        async def retrieve(
            self,
            tenant_id: uuid.UUID,
            query: str,
            *,
            limit: int = 4,
        ):
            del tenant_id, query, limit
            return []

    async def empty_retriever(*_args: object, **_kwargs: object) -> EmptyRetriever:
        return EmptyRetriever()

    monkeypatch.setattr(
        "app.services.channels.telegram.get_memory_retriever",
        empty_retriever,
    )

    first = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1101, text="Первый вопрос"),
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    closed = client.post(
        f"/api/v1/conversations/{conversation_id}/close",
        headers=auth_headers(),
    )
    assert closed.status_code == 200
    assert closed.json()["conversation"]["status"] == "closed"

    second_payload = telegram_payload(update_id=1102, text="Новый вопрос после закрытия")
    second_payload["message"]["message_id"] = 502
    second = client.post("/api/v1/channels/webhook/telegram", json=second_payload)

    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    assert asyncio.run(count_rows(session_factory, Conversation)) == 1
    thread = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth_headers(),
    )
    assert thread.status_code == 200
    assert thread.json()["status"] in {"auto", "escalated"}
    inbound_texts = [
        message["text"]
        for message in thread.json()["messages"]
        if message["direction"] == "inbound"
    ]
    assert sorted(inbound_texts) == sorted(
        [
            "Первый вопрос",
            "Новый вопрос после закрытия",
        ]
    )


def test_telegram_standalone_greeting_gets_ai_reply_without_knowledge(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    class EmptyRetriever:
        async def retrieve(
            self,
            tenant_id: uuid.UUID,
            query: str,
            *,
            limit: int = 4,
        ):
            del tenant_id, query, limit
            return []

    async def empty_retriever(*_args: object, **_kwargs: object) -> EmptyRetriever:
        return EmptyRetriever()

    monkeypatch.setattr(
        "app.services.channels.telegram.get_memory_retriever",
        empty_retriever,
    )
    response = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1110, text="Привет!"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "auto_reply"
    thread = client.get(
        f"/api/v1/conversations/{response.json()['conversation_id']}",
        headers=auth_headers(),
    ).json()
    assert thread["status"] == "auto"
    assert [message["sender_type"] for message in thread["messages"]] == [
        "customer",
        "ai",
    ]


def test_mtproto_inbound_auto_reply_uses_mtproto_delivery(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    created = client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    channel_id = uuid.UUID(created.json()["id"])

    async def mark_as_mtproto() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, channel_id)
            assert channel is not None
            channel.settings = {**channel.settings, "transport": "mtproto"}
            await session.commit()

    calls: list[tuple[uuid.UUID, str, str]] = []

    async def fake_mtproto_delivery(
        channel: Channel,
        peer_id: str,
        text: str,
        **_kwargs: object,
    ) -> object:
        calls.append((channel.id, peer_id, text))
        return SimpleNamespace(delivered=True, message_id=101)

    async def unexpected_bot_delivery(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("MTProto channel must not use Telegram Bot API delivery")

    asyncio.run(mark_as_mtproto())
    monkeypatch.setattr(
        "app.services.channels.telegram_mtproto.send_mtproto_message",
        fake_mtproto_delivery,
    )
    monkeypatch.setattr(
        "app.services.channels.telegram.send_telegram_message",
        unexpected_bot_delivery,
    )

    response = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1006),
    )

    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "auto_reply"
    assert len(calls) == 1
    assert calls[0][0] == channel_id
    assert calls[0][1] == "7001"

    thread = client.get(
        f"/api/v1/conversations/{response.json()['conversation_id']}",
        headers=auth_headers(),
    ).json()
    outbound = next(message for message in thread["messages"] if message["direction"] == "outbound")
    assert outbound["sender_type"] == "ai"
    assert outbound["status"] == "sent"
    assert outbound["ai_meta"]["delivery"] == "telegram-mtproto"
    assert outbound["ai_meta"]["telegram_message_id"] == 101


def test_disabled_bot_delivery_keeps_auto_reply_pending(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    monkeypatch.setattr(settings, "TELEGRAM_DELIVERY_ENABLED", False)

    response = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1007),
    )

    assert response.status_code == 200, response.text
    thread = client.get(
        f"/api/v1/conversations/{response.json()['conversation_id']}",
        headers=auth_headers(),
    ).json()
    outbound = next(message for message in thread["messages"] if message["direction"] == "outbound")
    assert outbound["status"] == "pending"
    assert outbound["ai_meta"]["delivery"] == "delivery-disabled"


def test_new_inbound_keeps_escalated_conversation_waiting_for_manager(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    first = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1008, text="Нужна нестандартная интеграция"),
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    client.post(
        f"/api/v1/conversations/{conversation_id}/escalate",
        headers=auth_headers(),
    )

    second = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1009, text="Жду ответа менеджера"),
    )

    assert second.status_code == 200, second.text
    assert second.json()["decision"] == "escalate"
    thread = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth_headers(),
    ).json()
    assert thread["status"] == "escalated"
    assert [message["sender_type"] for message in thread["messages"]] == [
        "customer",
        "customer",
    ]
    outbox = asyncio.run(escalation_outbox(session_factory))
    assert len(outbox) == 1
    assert outbox[0].metadata_json["conversation_id"] == conversation_id

    third = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1011, text="Есть ещё вопрос"),
    )
    assert third.status_code == 200, third.text
    assert len(asyncio.run(escalation_outbox(session_factory))) == 1


def test_hundred_percent_auto_reply_retries_an_escalated_conversation(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(
        seed_tenant(
            session_factory,
            auto_reply_enabled=True,
            confidence_threshold=100,
        )
    )
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    first = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1012, text="Нужна нестандартная интеграция"),
    )
    conversation_id = first.json()["conversation_id"]
    assert first.json()["decision"] == "escalate"

    second = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1013, text="Сколько занимает подключение Telegram?"),
    )

    assert second.status_code == 200, second.text
    assert second.json()["decision"] == "auto_reply"
    thread = client.get(
        f"/api/v1/conversations/{conversation_id}",
        headers=auth_headers(),
    ).json()
    assert thread["status"] == "auto"


def test_provider_failure_safely_escalates_inbound(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )

    async def fail_answer(*_args: object, **_kwargs: object) -> object:
        from app.services.rag.llm import LLMProviderRequestError

        raise LLMProviderRequestError("provider unavailable")

    monkeypatch.setattr(
        "app.services.ml.service.MLMessageService.answer",
        fail_answer,
    )
    response = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1010, text="Нужен ответ"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "escalate"
    thread = client.get(
        f"/api/v1/conversations/{response.json()['conversation_id']}",
        headers=auth_headers(),
    ).json()
    assert thread["status"] == "escalated"
    assert thread["messages"][-1]["ai_meta"]["ai_error"] == "LLMProviderRequestError"


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


def test_telegram_inbound_reuses_answered_conversation(
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

    async def mark_answered() -> None:
        async with session_factory() as session:
            conversation = await session.get(
                Conversation,
                uuid.UUID(first.json()["conversation_id"]),
            )
            assert conversation is not None
            conversation.status = "answered"
            await session.commit()

    asyncio.run(mark_answered())
    second = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1002, text="Рђ РµСЃР»Рё РµСЃС‚СЊ РµС‰С‘ РІРѕРїСЂРѕСЃ?"),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == first.json()["conversation_id"]
    assert asyncio.run(count_rows(session_factory, Conversation)) == 1


def test_completed_inbound_worker_is_idempotent(
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
    inbound_message_id = uuid.UUID(response.json()["inbound_message_id"])

    async def retry_job() -> None:
        async with session_factory() as session:
            result = await process_telegram_inbound_message(session, inbound_message_id)
            assert result.duplicate is True
            assert result.decision == "auto_reply"

    asyncio.run(retry_job())
    assert asyncio.run(count_rows(session_factory, Message)) == 2


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


def test_secretless_telegram_webhook_is_closed_outside_local_test(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    created = client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    webhook_path = created.json()["settings"]["webhook_path"]
    webhook_secret = webhook_path.rsplit("/", 1)[1]
    monkeypatch.setattr(settings, "APP_ENV", "production")

    closed = client.post("/api/v1/channels/webhook/telegram", json=telegram_payload())
    accepted = client.post(
        "/api/v1/channels/webhook/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
        json=telegram_payload(update_id=1004),
    )
    mismatch = client.post(
        webhook_path,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        json=telegram_payload(update_id=1005),
    )

    assert closed.status_code == 404
    assert accepted.status_code == 200, accepted.text
    assert mismatch.status_code == 401


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


def test_escalation_sends_customer_acknowledgement_and_records_reason(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=False))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    delivered: list[str] = []

    async def capture_message(_channel: Channel, _chat_id: str, text: str) -> bool:
        delivered.append(text)
        return True

    monkeypatch.setattr(settings, "TELEGRAM_DELIVERY_ENABLED", True)
    monkeypatch.setattr("app.services.channels.telegram.send_telegram_message", capture_message)

    response = client.post(
        "/api/v1/channels/webhook/telegram",
        json=telegram_payload(update_id=1999, text="Сколько стоит доставка?"),
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "escalate"
    assert any("передал ваш вопрос менеджеру" in text for text in delivered)

    async def decision_reason() -> str:
        from app.models.ops import AIDecisionEvent

        async with session_factory() as session:
            result = await session.execute(select(AIDecisionEvent.reason))
            return str(result.scalar_one())

    assert asyncio.run(decision_reason()) == "auto_reply_disabled"


def test_chat_rate_limit_runs_before_llm_and_deduplicates_webhook(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory, auto_reply_enabled=True))
    client.post(
        "/api/v1/channels",
        headers=auth_headers(),
        json={"type": "telegram", "bot_token": "1234567890:telegram-token"},
    )
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_RATE_LIMIT_PER_MINUTE", 1)
    calls = 0
    original = MLMessageService.answer

    async def count_answer(self: MLMessageService, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(MLMessageService, "answer", count_answer)
    first = client.post("/api/v1/channels/webhook/telegram", json=telegram_payload(update_id=2001))
    second_payload = telegram_payload(update_id=2002)
    second = client.post("/api/v1/channels/webhook/telegram", json=second_payload)
    duplicate = client.post("/api/v1/channels/webhook/telegram", json=second_payload)

    assert first.json()["decision"] == "auto_reply"
    assert second.json()["decision"] == "escalate"
    assert duplicate.json()["duplicate"] is True
    assert calls == 1
