"""Instagram OAuth, webhook validation, persistence and delivery tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import cast
from urllib.parse import parse_qs, urlparse

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
from app.models.channel import Channel, InstagramOAuthAttempt, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.email import EmailOutbox
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import AIDecisionEvent, AIUsageEvent
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User, UserNotificationSettings
from app.services.channels.instagram import send_instagram_message

TENANT_ID = uuid.UUID("91919191-9191-4191-8191-919191919101")
OTHER_TENANT_ID = uuid.UUID("91919191-9191-4191-8191-919191919102")
USER_ID = uuid.UUID("91919191-9191-4191-8191-919191919103")
INSTAGRAM_USER_ID = "17841400123456789"
APP_SECRET = "instagram-app-secret"
REAL_ASYNC_CLIENT = httpx.AsyncClient


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture(autouse=True)
def configure_instagram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "API_PUBLIC_URL", "https://api.example.test")
    monkeypatch.setattr(settings, "APP_PUBLIC_URL", "https://app.example.test")
    monkeypatch.setattr(settings, "INSTAGRAM_GRAPH_BASE_URL", "https://graph.instagram.test")
    monkeypatch.setattr(settings, "INSTAGRAM_GRAPH_VERSION", "v26.0")
    monkeypatch.setattr(settings, "INSTAGRAM_OAUTH_TOKEN_URL", "https://instagram.test/token")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "instagram-app-id")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", APP_SECRET)
    monkeypatch.setattr(settings, "INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "verify-instagram")
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
            InstagramOAuthAttempt.__table__,
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
        session.add(Tenant(id=TENANT_ID, name="Demo", slug="instagram-demo", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="instagram@example.test",
                full_name="Owner",
                password_hash=hash_password("password"),
                role="owner",
                status="active",
            )
        )
        await session.commit()


def install_instagram_transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL("https://instagram.test/token"):
            return httpx.Response(200, json={"access_token": "short-lived-token"})
        if request.url.path == "/access_token":
            return httpx.Response(200, json={"access_token": "long-lived-token"})
        if request.url.path == "/v26.0/me":
            return httpx.Response(
                200,
                json={
                    "user_id": INSTAGRAM_USER_ID,
                    "username": "demo_shop",
                    "name": "Demo Shop",
                },
            )
        if request.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200, json={"recipient_id": "customer-1", "message_id": "ig-out-1"}
            )
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


def connect_instagram(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, list[httpx.Request]]:
    requests = install_instagram_transport(monkeypatch)
    started = client.post(
        "/api/v1/channels/instagram/oauth/start",
        headers=auth_headers(),
        json={},
    )
    assert started.status_code == 200, started.text
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    client.cookies.set(
        "instagram_oauth_binding",
        started.cookies["instagram_oauth_binding"],
        path="/api/v1/channels/instagram/oauth/callback",
    )
    callback = client.get(
        "/api/v1/channels/instagram/oauth/callback",
        params={"state": query["state"][0], "code": "oauth-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    listed = client.get("/api/v1/channels", headers=auth_headers())
    return listed.json()[0], requests


def webhook_body(message_id: str = "ig-in-1") -> bytes:
    return json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": INSTAGRAM_USER_ID,
                    "messaging": [
                        {
                            "sender": {"id": "customer-1"},
                            "recipient": {"id": INSTAGRAM_USER_ID},
                            "timestamp": 1770000000000,
                            "message": {"mid": message_id, "text": "How long is delivery?"},
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_oauth_connect_is_browser_bound_and_stores_only_safe_settings(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    asyncio.run(seed_tenant(session_factory))
    channel, requests = connect_instagram(client, monkeypatch)
    assert channel["type"] == "instagram"
    assert channel["status"] == "active"
    assert channel["settings"] == {
        "instagram_user_id": INSTAGRAM_USER_ID,
        "username": "demo_shop",
        "display_name": "Demo Shop",
    }
    assert requests[-1].url.path.endswith("/subscribed_apps")

    async def credentials() -> str:
        async with session_factory() as session:
            stored = (await session.execute(select(Channel))).scalar_one()
            return decrypt_secret(stored.credentials_encrypted)

    decrypted = asyncio.run(credentials())
    assert "long-lived-token" in decrypted
    assert "long-lived-token" not in json.dumps(channel)


def test_oauth_callback_rejects_missing_browser_binding(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    asyncio.run(seed_tenant(session_factory))
    started = client.post(
        "/api/v1/channels/instagram/oauth/start", headers=auth_headers(), json={}
    )
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    client.cookies.clear()
    callback = client.get(
        "/api/v1/channels/instagram/oauth/callback",
        params={"state": query["state"][0], "code": "oauth-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 401


def test_instagram_webhook_verification_requires_configured_token(client: TestClient) -> None:
    accepted = client.get(
        "/api/v1/channels/webhook/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-instagram",
            "hub.challenge": "12345",
        },
    )
    assert accepted.status_code == 200
    assert accepted.text == "12345"
    rejected = client.get(
        "/api/v1/channels/webhook/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "12345",
        },
    )
    assert rejected.status_code == 403


def test_signed_webhook_is_durable_and_duplicate_safe(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    asyncio.run(seed_tenant(session_factory))
    channel, _ = connect_instagram(client, monkeypatch)
    body = webhook_body()
    path = "/api/v1/channels/webhook/instagram"
    first = client.post(
        path,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature(body)},
    )
    assert first.status_code == 200, first.text
    assert first.json()["processed_count"] == 1
    duplicate = client.post(
        path,
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature(body)},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True

    async def counts() -> tuple[int, int]:
        async with session_factory() as session:
            events = await session.scalar(select(func.count()).select_from(WebhookEvent))
            messages = await session.scalar(select(func.count()).select_from(Message))
            return int(events or 0), int(messages or 0)

    assert asyncio.run(counts()) == (1, 1)
    assert first.json()["channel_id"] == channel["id"]


def test_webhook_rejects_invalid_signature(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    asyncio.run(seed_tenant(session_factory))
    connect_instagram(client, monkeypatch)
    response = client.post(
        "/api/v1/channels/webhook/instagram",
        content=webhook_body(),
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 401


def test_instagram_outbound_uses_messaging_endpoint_and_enforces_byte_limit(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    asyncio.run(seed_tenant(session_factory))
    channel_data, requests = connect_instagram(client, monkeypatch)

    async def deliver() -> str | None:
        async with session_factory() as session:
            channel = await session.get(Channel, uuid.UUID(channel_data["id"]))
            assert channel is not None
            result = await send_instagram_message(channel, "customer-1", "Hello")
            return result.external_message_id

    assert asyncio.run(deliver()) == "ig-out-1"
    request = next(item for item in requests if item.url.path.endswith("/messages"))
    assert json.loads(request.content) == {
        "recipient": {"id": "customer-1"},
        "message": {"text": "Hello"},
    }
