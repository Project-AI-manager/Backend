"""MAX Bot API connection, webhook and delivery tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.core.secrets import decrypt_secret
from app.core.security import create_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.email import EmailOutbox
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import AIDecisionEvent, AIUsageEvent
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User, UserNotificationSettings
from app.services.channels.max import send_max_message

TENANT_ID = uuid.UUID("92929292-9292-4292-8292-929292929201")
OTHER_TENANT_ID = uuid.UUID("92929292-9292-4292-8292-929292929202")
USER_ID = uuid.UUID("92929292-9292-4292-8292-929292929203")
BOT_ID = "70000001"
BOT_TOKEN = "max-bot-token-long-lived"
REAL_ASYNC_CLIENT = httpx.AsyncClient


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture(autouse=True)
def configure_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "API_PUBLIC_URL", "https://api.example.test")
    monkeypatch.setattr(settings, "MAX_API_BASE_URL", "https://platform-api.max.test")
    monkeypatch.setattr(settings, "EMAIL_SEND_ENABLED", False)
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)


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
            KbCandidate.__table__,
        ):
            await connection.run_sync(create_table, table)
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


async def seed_tenant(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Demo", slug="max-demo", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="max@example.test",
                full_name="Owner",
                password_hash=hash_password("password"),
                role="owner",
                status="active",
            )
        )
        await session.commit()


def install_max_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bot_id: str = BOT_ID,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/me":
            return httpx.Response(
                200,
                json={
                    "user_id": int(bot_id),
                    "first_name": "MAX Support",
                    "username": "max_support_bot",
                    "is_bot": True,
                },
            )
        if request.url.path == "/subscriptions":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/messages":
            return httpx.Response(200, json={"body": {"mid": "max-out-1"}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: REAL_ASYNC_CLIENT(
            transport=transport,
            timeout=kwargs.get("timeout"),
        ),
    )
    return requests


def connect_max(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    replace_channel_id: str | None = None,
) -> tuple[dict, list[httpx.Request]]:
    requests = install_max_transport(monkeypatch)
    body: dict[str, object] = {"bot_token": BOT_TOKEN, "name": "MAX Support"}
    if replace_channel_id:
        body["replace_channel_id"] = replace_channel_id
    response = client.post("/api/v1/channels/max", headers=auth_headers(), json=body)
    assert response.status_code == 200, response.text
    return response.json(), requests


def webhook_payload(message_id: str = "max-in-1") -> dict:
    return {
        "update_type": "message_created",
        "timestamp": 1770000000000,
        "message": {
            "sender": {
                "user_id": 80000001,
                "first_name": "Ivan",
                "last_name": "Client",
                "is_bot": False,
            },
            "recipient": {"user_id": int(BOT_ID)},
            "body": {"mid": message_id, "text": "How long is delivery?"},
        },
    }


async def webhook_secret(factory: async_sessionmaker[AsyncSession], channel_id: str) -> str:
    async with factory() as session:
        channel = await session.get(Channel, uuid.UUID(channel_id))
        assert channel is not None
        payload = json.loads(decrypt_secret(channel.credentials_encrypted))
        return str(payload["webhook_secret"])


def test_connect_probes_bot_and_registers_secret_webhook(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, requests = connect_max(client, monkeypatch)
    assert channel["type"] == "max"
    assert channel["settings"] == {
        "bot_id": BOT_ID,
        "username": "max_support_bot",
        "display_name": "MAX Support",
    }
    subscription = next(item for item in requests if item.url.path == "/subscriptions")
    payload = json.loads(subscription.content)
    assert payload["url"] == f"https://api.example.test/api/v1/channels/webhook/max/{channel['id']}"
    assert payload["update_types"] == ["message_created", "bot_started"]
    assert payload["secret"]
    assert subscription.headers["Authorization"] == BOT_TOKEN
    assert BOT_TOKEN not in json.dumps(channel)


def test_reconnect_rotates_credentials_on_the_same_channel(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    first, _ = connect_max(client, monkeypatch)
    second, _ = connect_max(
        client,
        monkeypatch,
        replace_channel_id=first["id"],
    )
    assert second["id"] == first["id"]


def test_webhook_requires_exact_secret_header(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, _ = connect_max(client, monkeypatch)
    path = f"/api/v1/channels/webhook/max/{channel['id']}"
    missing = client.post(path, json=webhook_payload())
    assert missing.status_code == 401
    invalid = client.post(
        path,
        json=webhook_payload(),
        headers={"X-Max-Bot-Api-Secret": "wrong-secret"},
    )
    assert invalid.status_code == 401


def test_webhook_is_durable_and_duplicate_safe(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, _ = connect_max(client, monkeypatch)
    secret = asyncio.run(webhook_secret(session_factory, channel["id"]))
    path = f"/api/v1/channels/webhook/max/{channel['id']}"
    headers = {"X-Max-Bot-Api-Secret": secret}
    first = client.post(path, json=webhook_payload(), headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["processed_count"] == 1
    duplicate = client.post(path, json=webhook_payload(), headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    async def counts() -> tuple[int, int]:
        async with session_factory() as session:
            events = await session.scalar(select(func.count()).select_from(WebhookEvent))
            messages = await session.scalar(select(func.count()).select_from(Message))
            return int(events or 0), int(messages or 0)

    assert asyncio.run(counts()) == (1, 1)


def test_bot_echo_and_attachment_only_update_are_acknowledged_without_persistence(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, _ = connect_max(client, monkeypatch)
    secret = asyncio.run(webhook_secret(session_factory, channel["id"]))
    path = f"/api/v1/channels/webhook/max/{channel['id']}"
    payload = webhook_payload()
    payload["message"]["sender"]["is_bot"] = True
    response = client.post(
        path,
        json=payload,
        headers={"X-Max-Bot-Api-Secret": secret},
    )
    assert response.status_code == 200
    assert response.json()["inbound_message_id"] is None


def test_max_outbound_uses_user_id_and_text_body(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel_data, requests = connect_max(client, monkeypatch)

    async def deliver() -> str | None:
        async with session_factory() as session:
            channel = await session.get(Channel, uuid.UUID(channel_data["id"]))
            assert channel is not None
            result = await send_max_message(channel, "80000001", "Hello")
            return result.external_message_id

    assert asyncio.run(deliver()) == "max-out-1"
    request = next(item for item in requests if item.url.path == "/messages")
    assert request.url.params["user_id"] == "80000001"
    assert json.loads(request.content) == {"text": "Hello"}
