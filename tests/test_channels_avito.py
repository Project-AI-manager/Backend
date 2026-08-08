"""Avito OAuth, webhook and Messenger API tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.core.secrets import decrypt_secret
from app.core.security import create_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.channel import AvitoOAuthAttempt, Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.email import EmailOutbox
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import AIDecisionEvent, AIUsageEvent
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User, UserNotificationSettings
from app.services.channels.avito import poll_avito_channels

TENANT_ID = uuid.UUID("77777777-7777-4777-8777-777777777701")
USER_ID = uuid.UUID("77777777-7777-4777-8777-777777777702")
AVITO_USER_ID = "12345678"


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture(autouse=True)
def configure_avito(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AVITO_CLIENT_ID", "avito-client")
    monkeypatch.setattr(settings, "AVITO_CLIENT_SECRET", "avito-secret")
    monkeypatch.setattr(settings, "API_PUBLIC_URL", "https://api.example.test")
    monkeypatch.setattr(settings, "APP_PUBLIC_URL", "https://example.test")
    monkeypatch.setattr(settings, "EMAIL_SEND_ENABLED", False)
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as connection:
        for table in (
            Tenant.__table__,
            TenantAIConfig.__table__,
            User.__table__,
            UserNotificationSettings.__table__,
            EmailOutbox.__table__,
            Channel.__table__,
            AvitoOAuthAttempt.__table__,
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


async def seed_tenant(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Demo", slug="avito-demo", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="owner@example.test",
                full_name="Owner",
                password_hash=hash_password("password"),
                role="owner",
                status="active",
            )
        )
        session.add(
            TenantAIConfig(
                tenant_id=TENANT_ID,
                llm_provider="mock",
                auto_reply_enabled=True,
                confidence_threshold=0,
            )
        )
        await session.commit()


def auth_headers() -> dict[str, str]:
    token = create_token(USER_ID, tenant_id=TENANT_ID, role="owner")
    return {"Authorization": f"Bearer {token}"}


def install_avito_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "avito-access-token",
                    "refresh_token": "avito-refresh-token",
                    "expires_in": 86400,
                },
            )
        if request.url.path == "/core/v1/accounts/self":
            return httpx.Response(200, json={"id": AVITO_USER_ID, "name": "Avito Demo"})
        if request.url.path == "/messenger/v3/webhook":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/messenger/v1/webhook/unsubscribe":
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/chats"):
            return httpx.Response(200, json={"chats": []})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"id": "avito.out.1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )
    return requests


def connect_avito(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, list, str]:
    requests = install_avito_transport(monkeypatch)
    start = client.post("/api/v1/channels/avito/oauth/start", headers=auth_headers())
    assert start.status_code == 200, start.text
    query = parse_qs(urlparse(start.json()["authorization_url"]).query)
    client.cookies.set(
        "avito_oauth_binding",
        start.cookies["avito_oauth_binding"],
        path="/api/v1/channels/avito/oauth/callback",
    )
    callback = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"code": "oauth-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    channels = client.get("/api/v1/channels", headers=auth_headers()).json()
    subscription = next(item for item in requests if item.url.path == "/messenger/v3/webhook")
    subscribed_url = str(json.loads(subscription.content)["url"])
    return channels[0], requests, urlparse(subscribed_url).path


def test_oauth_connects_account_and_registers_secret_webhook(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, requests, webhook_path = connect_avito(client, monkeypatch)
    assert channel["type"] == "avito"
    assert channel["settings"]["user_id"] == AVITO_USER_ID
    assert "webhook_path" not in channel["settings"]
    assert webhook_path.startswith("/api/v1/channels/webhook/avito/")
    subscription = next(item for item in requests if item.url.path == "/messenger/v3/webhook")
    assert subscription.headers["Authorization"] == "Bearer avito-access-token"
    assert (
        "https://api.example.test/api/v1/channels/webhook/avito/"
        in subscription.content.decode()
    )

    async def credentials() -> str:
        async with session_factory() as session:
            stored = await session.get(Channel, uuid.UUID(channel["id"]))
            assert stored is not None
            return decrypt_secret(stored.credentials_encrypted)

    assert "avito-refresh-token" in asyncio.run(credentials())


def test_invalid_oauth_state_is_rejected(
    client: TestClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    asyncio.run(seed_tenant(session_factory))
    response = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"code": "code", "state": "invalid-state-token"},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_oauth_state_is_browser_bound_and_one_time(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    install_avito_transport(monkeypatch)
    start = client.post("/api/v1/channels/avito/oauth/start", headers=auth_headers())
    state_token = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]
    binding = start.cookies["avito_oauth_binding"]

    client.cookies.set(
        "avito_oauth_binding",
        "wrong-browser-binding",
        path="/api/v1/channels/avito/oauth/callback",
    )
    wrong_browser = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"code": "oauth-code", "state": state_token},
        follow_redirects=False,
    )
    assert wrong_browser.status_code == 401

    client.cookies.set(
        "avito_oauth_binding",
        binding,
        path="/api/v1/channels/avito/oauth/callback",
    )
    first = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"code": "oauth-code", "state": state_token},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.headers["location"].endswith("/channels?avito=connected")

    client.cookies.set(
        "avito_oauth_binding",
        binding,
        path="/api/v1/channels/avito/oauth/callback",
    )
    replay = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"code": "another-code", "state": state_token},
        follow_redirects=False,
    )
    assert replay.status_code == 401


def test_oauth_cancel_redirects_and_consumes_state(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))
    start = client.post("/api/v1/channels/avito/oauth/start", headers=auth_headers())
    state_token = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]
    binding = start.cookies["avito_oauth_binding"]
    assert "HttpOnly" in start.headers["set-cookie"]
    assert "SameSite=lax" in start.headers["set-cookie"]
    client.cookies.set(
        "avito_oauth_binding",
        binding,
        path="/api/v1/channels/avito/oauth/callback",
    )
    cancelled = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"error": "access_denied", "state": state_token},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    assert cancelled.headers["location"].endswith("/channels?avito=cancelled")

    client.cookies.set(
        "avito_oauth_binding",
        binding,
        path="/api/v1/channels/avito/oauth/callback",
    )
    replay = client.get(
        "/api/v1/channels/avito/oauth/callback",
        params={"error": "access_denied", "state": state_token},
        follow_redirects=False,
    )
    assert replay.status_code == 401


def test_webhook_health_probe_text_ingest_reply_and_dedupe(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, requests, webhook_path = connect_avito(client, monkeypatch)
    health = client.post(webhook_path, json={})
    assert health.status_code == 200
    body = {
        "id": "event-1",
        "payload": {
            "type": "message",
            "value": {
                "id": "avito.in.1",
                "user_id": AVITO_USER_ID,
                "author_id": "customer-7",
                "author_name": "Анна",
                "chat_id": "chat-1",
                "type": "text",
                "content": {"text": "Как быстро доставка?"},
            },
        },
    }
    first = client.post(webhook_path, json=body)
    second = client.post(webhook_path, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["processed_count"] == 1
    assert first.json()["decision"] is None
    assert second.json()["duplicate"] is True
    assert len([item for item in requests if item.url.path.endswith("/messages")]) == 0

    async def process_persisted() -> dict:
        async with session_factory() as session:
            inbound = (
                await session.execute(
                    select(Message).where(Message.external_message_id == "avito.in.1")
                )
            ).scalar_one()
            from app.services.channels.telegram import process_channel_inbound_message

            return (await process_channel_inbound_message(session, inbound.id)).model_dump()

    result = asyncio.run(process_persisted())
    assert result["decision"] in {"auto_reply", "escalate"}


def test_polling_worker_processes_durable_webhook_inbound(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    _channel, _requests, webhook_path = connect_avito(client, monkeypatch)
    response = client.post(
        webhook_path,
        json={
            "id": "event-pending",
            "payload": {
                "type": "message",
                "value": {
                    "id": "avito.in.pending",
                    "user_id": AVITO_USER_ID,
                    "author_id": "customer-pending",
                    "chat_id": "chat-pending",
                    "type": "text",
                    "content": {"text": "Здравствуйте"},
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["decision"] is None

    async def run_worker() -> dict[str, int]:
        async with session_factory() as session:
            return await poll_avito_channels(session)

    result = asyncio.run(run_worker())
    assert result["processed_pending"] == 1


def test_manager_reply_uses_avito_text_endpoint(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, requests, _webhook_path = connect_avito(client, monkeypatch)

    async def seed_conversation() -> uuid.UUID:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Anna")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=uuid.UUID(channel["id"]),
                status="escalated",
            )
            session.add(conversation)
            await session.flush()
            session.add(
                Message(
                    tenant_id=TENANT_ID,
                    conversation_id=conversation.id,
                    direction="inbound",
                    sender_type="customer",
                    text="Question",
                    external_message_id="avito.in.manual",
                    status="received",
                    ai_meta={"source": "avito", "chat_id": "chat-manual"},
                )
            )
            await session.commit()
            return conversation.id

    conversation_id = asyncio.run(seed_conversation())
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/reply",
        headers=auth_headers(),
        json={"text": "Ответ менеджера"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"]["status"] == "sent"
    assert any("chat-manual/messages" in str(item.url) for item in requests)


def test_webhook_for_another_account_is_ignored(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, _requests, webhook_path = connect_avito(client, monkeypatch)
    response = client.post(
        webhook_path,
        json={
            "id": "event-cross-account",
            "payload": {
                "type": "message",
                "value": {
                    "id": "message-cross-account",
                    "user_id": "another-account",
                    "author_id": "customer",
                    "chat_id": "chat",
                    "type": "text",
                    "content": {"text": "ignore"},
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["processed_count"] == 0

    async def messages() -> list[Message]:
        async with session_factory() as session:
            return list((await session.execute(select(Message))).scalars().all())

    assert asyncio.run(messages()) == []


def test_disconnect_unsubscribes_and_invalidates_the_secret_path(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    channel, requests, webhook_path = connect_avito(client, monkeypatch)

    response = client.delete(f"/api/v1/channels/{channel['id']}", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert any(item.url.path == "/messenger/v1/webhook/unsubscribe" for item in requests)
    assert client.post(webhook_path, json={}).status_code == 404
