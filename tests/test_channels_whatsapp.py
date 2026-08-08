"""WhatsApp Business Cloud API channel tests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
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

TENANT_ID = uuid.UUID("66666666-6666-4666-8666-666666666601")
USER_ID = uuid.UUID("66666666-6666-4666-8666-666666666602")
PHONE_NUMBER_ID = "123456789"
APP_SECRET = "meta-app-secret"
VERIFY_TOKEN = "verify-me-please"


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture(autouse=True)
def disable_external_services(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def seed_tenant(
    session_factory: async_sessionmaker[AsyncSession], *, auto_reply: bool = True
) -> None:
    async with session_factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Demo", slug="wa-demo", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="wa@example.com",
                full_name="Owner",
                role="owner",
                password_hash=hash_password("password"),
                status="active",
            )
        )
        session.add(
            TenantAIConfig(
                tenant_id=TENANT_ID,
                auto_reply_enabled=auto_reply,
                confidence_threshold=50,
                llm_provider="mock",
                embedding_model="local",
            )
        )
        document = KbDocument(
            tenant_id=TENANT_ID, title="Delivery", source_type="manual", status="ready", version=1
        )
        session.add(document)
        await session.flush()
        session.add(
            KbChunk(
                tenant_id=TENANT_ID,
                document_id=document.id,
                text="How long is delivery? Delivery takes two days.",
                position=0,
                token_count=4,
                tags={},
                version=1,
            )
        )
        await session.commit()


def connect(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict:
    def probe_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.headers["Authorization"] == "Bearer long-lived-access-token"
            requested_id = "987654321" if "waba-2" in str(request.url) else PHONE_NUMBER_ID
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": requested_id,
                            "display_phone_number": "+7 999 000-11-22",
                            "verified_name": "Demo",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"messages": [{"id": "wamid.out.default"}]})

    transport = httpx.MockTransport(probe_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )
    response = client.post(
        "/api/v1/channels/whatsapp",
        headers=auth_headers(),
        json={
            "phone_number_id": PHONE_NUMBER_ID,
            "waba_id": "waba-1",
            "access_token": "long-lived-access-token",
            "app_secret": APP_SECRET,
            "verify_token": VERIFY_TOKEN,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def install_connect_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe_handler(request: httpx.Request) -> httpx.Response:
        requested_id = "987654321" if "waba-2" in str(request.url) else PHONE_NUMBER_ID
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": requested_id,
                        "display_phone_number": "+7 999 000-11-22",
                        "verified_name": "Demo",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(probe_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )


def payload(*, message_id: str = "wamid.in.1", text: str = "How long is delivery?") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": "79990001122", "profile": {"name": "Anna"}}],
                            "messages": [
                                {
                                    "from": "79990001122",
                                    "id": message_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def signed_post(client: TestClient, body: dict) -> httpx.Response:
    raw = json.dumps(body, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        f"/api/v1/channels/webhook/whatsapp/{PHONE_NUMBER_ID}",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def test_connect_encrypts_credentials_and_verifies_webhook(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = connect(client, monkeypatch)
    assert created["type"] == "whatsapp"
    assert created["settings"]["phone_number_id"] == PHONE_NUMBER_ID
    assert created["settings"]["display_phone_number"] == "+7 999 000-11-22"
    assert "access_token" not in created["settings"]

    async def stored() -> str:
        async with session_factory() as session:
            channel = await session.get(Channel, uuid.UUID(created["id"]))
            assert channel is not None
            return decrypt_secret(channel.credentials_encrypted)

    assert json.loads(asyncio.run(stored()))["access_token"] == "long-lived-access-token"
    verified = client.get(
        f"/api/v1/channels/webhook/whatsapp/{PHONE_NUMBER_ID}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge-value",
        },
    )
    assert verified.status_code == 200
    assert verified.text == "challenge-value"


def test_signed_text_webhook_auto_replies_and_deduplicates(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    connect(client, monkeypatch)
    monkeypatch.undo()
    sent: list[dict] = []

    def graph_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out.1"}]})

    transport = httpx.MockTransport(graph_handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(transport=transport, timeout=kwargs.get("timeout")),
    )
    first = signed_post(client, payload())
    second = signed_post(client, payload())
    assert first.status_code == 200, first.text
    assert first.json()["processed_count"] == 1
    assert first.json()["decision"] == "auto_reply"
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert len(sent) == 1

    async def state() -> tuple[int, Message | None]:
        async with session_factory() as session:
            count = await session.scalar(select(func.count()).select_from(WebhookEvent))
            result = await session.execute(
                select(Message).where(Message.external_message_id == "wamid.out.1")
            )
            return int(count or 0), result.scalar_one_or_none()

    count, outbound = asyncio.run(state())
    assert count == 1
    assert outbound is not None
    assert outbound.status == "sent"


def test_invalid_signature_is_rejected(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    connect(client, monkeypatch)
    response = client.post(
        f"/api/v1/channels/webhook/whatsapp/{PHONE_NUMBER_ID}",
        json=payload(),
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 401


def test_signed_payload_for_another_meta_account_is_ignored(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    connect(client, monkeypatch)
    body = payload(message_id="wamid.cross-tenant")
    body["entry"][0]["id"] = "another-waba"

    response = signed_post(client, body)
    assert response.status_code == 200
    assert response.json()["processed_count"] == 0

    async def message_count() -> int:
        async with session_factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(Message))).scalar_one()
            )

    assert asyncio.run(message_count()) == 0


def test_batch_webhook_processes_each_text_message(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    connect(client, monkeypatch)
    monkeypatch.undo()
    body = payload()
    messages = body["entry"][0]["changes"][0]["value"]["messages"]
    messages.append(
        {
            "from": "79990001122",
            "id": "wamid.in.2",
            "type": "text",
            "text": {"body": "How long is delivery?"},
        }
    )
    counter = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal counter
        counter += 1
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out.{counter}"}]})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(transport=transport, timeout=kwargs.get("timeout")),
    )
    response = signed_post(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["processed_count"] == 2
    assert counter == 2


def test_status_webhook_marks_outbound_read(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = connect(client, monkeypatch)

    async def seed_outbound() -> None:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Anna")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=uuid.UUID(created["id"]),
                status="auto",
            )
            session.add(conversation)
            await session.flush()
            session.add(
                Message(
                    tenant_id=TENANT_ID,
                    conversation_id=conversation.id,
                    direction="outbound",
                    sender_type="ai",
                    text="Hello",
                    external_message_id="wamid.out.status",
                    status="sent",
                )
            )
            await session.commit()

    asyncio.run(seed_outbound())
    status_payload = {
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "statuses": [{"id": "wamid.out.status", "status": "read"}],
                        }
                    }
                ]
            }
        ]
    }
    response = signed_post(client, status_payload)
    assert response.status_code == 200

    async def message_status() -> str:
        async with session_factory() as session:
            result = await session.execute(
                select(Message).where(Message.external_message_id == "wamid.out.status")
            )
            return result.scalar_one().status

    assert asyncio.run(message_status()) == "read"


def test_manager_reply_uses_whatsapp_cloud_api(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = connect(client, monkeypatch)

    async def seed_conversation() -> uuid.UUID:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Anna")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=uuid.UUID(created["id"]),
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
                    external_message_id="wamid.in.manual",
                    status="received",
                    ai_meta={"source": "whatsapp", "chat_id": "79990001122"},
                )
            )
            await session.commit()
            return conversation.id

    conversation_id = asyncio.run(seed_conversation())
    from app.services.channels.base import DeliveryResult

    async def fake_send(_channel: Channel, recipient: str, text: str) -> DeliveryResult:
        assert (recipient, text) == ("79990001122", "Manual answer")
        return DeliveryResult(
            delivered=True,
            external_message_id="wamid.out.manual",
            status="sent",
            metadata={"delivery": "whatsapp-cloud-api"},
        )

    monkeypatch.setattr("app.services.conversations.send_whatsapp_message", fake_send)
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/reply",
        headers=auth_headers(),
        json={"text": "Manual answer"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"]["status"] == "sent"
    assert response.json()["message"]["ai_meta"]["delivery"] == "channel-sent"


def test_probe_uses_graph_api(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = connect(client, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer long-lived-access-token"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": PHONE_NUMBER_ID,
                        "display_phone_number": "+7 999 000-11-22",
                        "verified_name": "Demo",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(transport=transport, timeout=kwargs.get("timeout")),
    )
    response = client.post(
        f"/api/v1/channels/whatsapp/{created['id']}/probe", headers=auth_headers()
    )
    assert response.status_code == 200
    assert response.json()["verified_name"] == "Demo"


def test_connect_rejects_invalid_meta_credentials_without_creating_channel(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))

    transport = httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": {}}))
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original_client(
            transport=transport, timeout=kwargs.get("timeout")
        ),
    )
    response = client.post(
        "/api/v1/channels/whatsapp",
        headers=auth_headers(),
        json={
            "phone_number_id": PHONE_NUMBER_ID,
            "waba_id": "waba-1",
            "access_token": "invalid-access-token",
            "app_secret": APP_SECRET,
            "verify_token": VERIFY_TOKEN,
        },
    )
    assert response.status_code == 502

    async def channel_count() -> int:
        async with session_factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(Channel))).scalar_one()
            )

    assert asyncio.run(channel_count()) == 0


def test_reconnect_rotates_credentials_for_the_owned_phone_number(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = connect(client, monkeypatch)

    install_connect_probe(monkeypatch)

    response = client.post(
        "/api/v1/channels/whatsapp",
        headers=auth_headers(),
        json={
            "phone_number_id": PHONE_NUMBER_ID,
            "waba_id": "waba-1",
            "access_token": "long-lived-access-token",
            "app_secret": "replacement-secret",
            "verify_token": "replacement-verify",
            "name": "WhatsApp replacement",
            "replace_channel_id": created["id"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == created["id"]
    assert response.json()["settings"]["phone_number_id"] == PHONE_NUMBER_ID

    async def channel_count() -> int:
        async with session_factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(Channel))).scalar_one()
            )

    assert asyncio.run(channel_count()) == 1


def test_reconnect_cannot_rebind_existing_conversations_to_another_phone(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created = connect(client, monkeypatch)

    response = client.post(
        "/api/v1/channels/whatsapp",
        headers=auth_headers(),
        json={
            "phone_number_id": "987654321",
            "waba_id": "waba-2",
            "access_token": "long-lived-access-token",
            "app_secret": "replacement-secret",
            "verify_token": "replacement-verify",
            "replace_channel_id": created["id"],
        },
    )
    assert response.status_code == 409
