"""VK Community Callback API and message delivery tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import cast
from urllib.parse import parse_qs

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
from app.services.channels.vk import send_vk_message

TENANT_ID = uuid.UUID("88888888-8888-4888-8888-888888888801")
OTHER_TENANT_ID = uuid.UUID("88888888-8888-4888-8888-888888888802")
USER_ID = uuid.UUID("88888888-8888-4888-8888-888888888803")
GROUP_ID = 123456
ACCESS_TOKEN = "vk-community-access-token-long-lived"
CALLBACK_CONFIRMATION = "confirmation-value"
CALLBACK_SECRET = "vk-callback-secret"
REAL_ASYNC_CLIENT = httpx.AsyncClient


def create_table(sync_connection: Connection, table: object) -> None:
    cast(Table, table).create(sync_connection)


@pytest.fixture(autouse=True)
def configure_vk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "API_PUBLIC_URL", "https://api.example.test")
    monkeypatch.setattr(settings, "VK_API_BASE_URL", "https://api.vk.test/method")
    monkeypatch.setattr(settings, "VK_API_VERSION", "5.131")
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


async def seed_tenant(
    factory: async_sessionmaker[AsyncSession], *, auto_reply: bool = True
) -> None:
    async with factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Demo", slug="vk-demo", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="vk@example.test",
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
                auto_reply_enabled=auto_reply,
                confidence_threshold=100,
                embedding_model="local",
            )
        )
        document = KbDocument(
            tenant_id=TENANT_ID,
            title="Delivery",
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
                text="How long is delivery? Delivery takes two days.",
                position=0,
                token_count=8,
                tags={},
                version=1,
            )
        )
        await session.commit()


def install_vk_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_id: int = GROUP_ID,
    send_responses: list[dict] | None = None,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    queued_send_responses = list(send_responses or [{"response": 7001}])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/groups.getById"):
            return httpx.Response(
                200,
                json={
                    "response": [{"id": group_id, "name": "Demo Community", "screen_name": "demo"}]
                },
            )
        if request.url.path.endswith("/messages.send"):
            payload = queued_send_responses.pop(0) if queued_send_responses else {"response": 7001}
            return httpx.Response(200, json=payload)
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


def connect_vk(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_id: int = GROUP_ID,
    replace_channel_id: str | None = None,
) -> tuple[dict, list[httpx.Request]]:
    requests = install_vk_transport(monkeypatch, group_id=group_id)
    body: dict[str, object] = {
        "group_id": group_id,
        "access_token": ACCESS_TOKEN,
        "callback_confirmation": CALLBACK_CONFIRMATION,
        "callback_secret": CALLBACK_SECRET,
        "name": "VK Support",
    }
    if replace_channel_id:
        body["replace_channel_id"] = replace_channel_id
    response = client.post("/api/v1/channels/vk", headers=auth_headers(), json=body)
    assert response.status_code == 200, response.text
    return response.json(), requests


def callback_payload(
    *,
    message_id: int = 901,
    event_id: str = "event-1",
    peer_id: int = 70001,
    from_id: int = 70001,
    text: str = "How long is delivery?",
    out: int = 0,
    attachments: list[dict] | None = None,
) -> dict:
    return {
        "type": "message_new",
        "event_id": event_id,
        "group_id": GROUP_ID,
        "secret": CALLBACK_SECRET,
        "object": {
            "message": {
                "id": message_id,
                "conversation_message_id": message_id + 100,
                "peer_id": peer_id,
                "from_id": from_id,
                "text": text,
                "out": out,
                "attachments": attachments or [],
            }
        },
    }


def test_connect_probes_with_bearer_form_version_encrypts_secrets_and_returns_callback(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)

    probe = requests[0]
    assert probe.url.path.endswith("/groups.getById")
    assert probe.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    form = parse_qs(probe.content.decode())
    assert form["group_id"] == [str(GROUP_ID)]
    assert form["v"] == ["5.131"]
    assert created["type"] == "vk"
    assert created["settings"] == {
        "group_id": GROUP_ID,
        "group_name": "Demo Community",
        "screen_name": "demo",
        "callback_url": f"https://api.example.test/api/v1/channels/webhook/vk/{created['id']}",
    }
    serialized = json.dumps(created)
    assert ACCESS_TOKEN not in serialized
    assert CALLBACK_SECRET not in serialized
    assert CALLBACK_CONFIRMATION not in serialized

    async def stored_credentials() -> tuple[str, str]:
        async with session_factory() as session:
            channel = await session.get(Channel, uuid.UUID(created["id"]))
            assert channel is not None
            return channel.credentials_encrypted, decrypt_secret(channel.credentials_encrypted)

    encrypted, decrypted = asyncio.run(stored_credentials())
    assert ACCESS_TOKEN not in encrypted
    assert CALLBACK_SECRET not in encrypted
    assert json.loads(decrypted) == {
        "access_token": ACCESS_TOKEN,
        "callback_confirmation": CALLBACK_CONFIRMATION,
        "callback_secret": CALLBACK_SECRET,
    }


def test_connect_rejects_probe_error_and_group_mismatch_without_persisting(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"error": {"error_code": 27, "error_msg": "Group authorization failed"}},
        )
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: REAL_ASYNC_CLIENT(
            transport=transport,
            timeout=kwargs.get("timeout"),
        ),
    )
    invalid = client.post(
        "/api/v1/channels/vk",
        headers=auth_headers(),
        json={
            "group_id": GROUP_ID,
            "access_token": ACCESS_TOKEN,
            "callback_confirmation": CALLBACK_CONFIRMATION,
            "callback_secret": CALLBACK_SECRET,
        },
    )
    assert invalid.status_code == 502

    monkeypatch.undo()
    install_vk_transport(monkeypatch, group_id=GROUP_ID + 1)
    mismatch = client.post(
        "/api/v1/channels/vk",
        headers=auth_headers(),
        json={
            "group_id": GROUP_ID,
            "access_token": ACCESS_TOKEN,
            "callback_confirmation": CALLBACK_CONFIRMATION,
            "callback_secret": CALLBACK_SECRET,
        },
    )
    assert mismatch.status_code == 422

    async def channel_count() -> int:
        async with session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(Channel)) or 0)

    assert asyncio.run(channel_count()) == 0


def test_reconnect_rotates_only_the_same_community(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, _requests = connect_vk(client, monkeypatch)

    same, _requests = connect_vk(
        client,
        monkeypatch,
        replace_channel_id=created["id"],
    )
    assert same["id"] == created["id"]

    other_group = GROUP_ID + 1
    install_vk_transport(monkeypatch, group_id=other_group)
    rejected = client.post(
        "/api/v1/channels/vk",
        headers=auth_headers(),
        json={
            "group_id": other_group,
            "access_token": ACCESS_TOKEN,
            "callback_confirmation": CALLBACK_CONFIRMATION,
            "callback_secret": "replacement-secret",
            "replace_channel_id": created["id"],
        },
    )
    assert rejected.status_code == 409

    async def channel_count() -> int:
        async with session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(Channel)) or 0)

    assert asyncio.run(channel_count()) == 1


def test_confirmation_is_exact_plain_text_and_invalid_binding_is_rejected(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, _requests = connect_vk(client, monkeypatch)
    callback_url = created["settings"]["callback_url"]
    callback_path = callback_url.removeprefix("https://api.example.test")

    confirmed = client.post(
        callback_path,
        json={
            "type": "confirmation",
            "group_id": GROUP_ID,
            "secret": CALLBACK_SECRET,
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.text == CALLBACK_CONFIRMATION
    assert confirmed.headers["content-type"].startswith("text/plain")

    wrong_group = client.post(
        callback_path,
        json={
            "type": "confirmation",
            "group_id": GROUP_ID + 1,
            "secret": CALLBACK_SECRET,
        },
    )
    assert wrong_group.status_code == 403
    wrong_secret = client.post(
        callback_path,
        json={
            "type": "confirmation",
            "group_id": GROUP_ID,
            "secret": "wrong-secret",
        },
    )
    assert wrong_secret.status_code == 403


def test_message_new_is_durable_fast_and_deduplicates_provider_message_id(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)
    callback_path = created["settings"]["callback_url"].removeprefix("https://api.example.test")

    first = client.post(callback_path, json=callback_payload())
    duplicate = client.post(
        callback_path,
        json=callback_payload(event_id="another-event-for-the-same-message"),
    )
    assert first.status_code == 200, first.text
    assert first.text == "ok"
    assert duplicate.status_code == 200
    assert duplicate.text == "ok"
    assert len([request for request in requests if request.url.path.endswith("messages.send")]) == 0

    async def durable_state() -> tuple[int, int, Message, WebhookEvent]:
        async with session_factory() as session:
            message_count = int(
                await session.scalar(select(func.count()).select_from(Message)) or 0
            )
            event_count = int(
                await session.scalar(select(func.count()).select_from(WebhookEvent)) or 0
            )
            inbound = (await session.execute(select(Message))).scalar_one()
            event = (await session.execute(select(WebhookEvent))).scalar_one()
            return message_count, event_count, inbound, event

    message_count, event_count, inbound, event = asyncio.run(durable_state())
    assert (message_count, event_count) == (1, 1)
    assert inbound.external_message_id == "message:901"
    assert inbound.ai_meta["source"] == "vk"
    assert inbound.ai_meta["chat_id"] == "70001"
    assert "decision" not in inbound.ai_meta
    assert event.external_event_id == "message:901"
    assert event.processed is False


@pytest.mark.parametrize(
    ("payload"),
    [
        callback_payload(message_id=910, out=1),
        callback_payload(message_id=911, peer_id=2_000_000_001, from_id=70001),
        callback_payload(
            message_id=912,
            text="",
            attachments=[{"type": "photo", "photo": {"id": 1}}],
        ),
    ],
    ids=["outgoing", "group-chat", "media-only"],
)
def test_unsupported_message_variants_are_acknowledged_and_ignored(
    payload: dict,
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, _requests = connect_vk(client, monkeypatch)
    callback_path = created["settings"]["callback_url"].removeprefix("https://api.example.test")

    response = client.post(callback_path, json=payload)
    assert response.status_code == 200
    assert response.text == "ok"

    async def counts() -> tuple[int, int]:
        async with session_factory() as session:
            messages = int(await session.scalar(select(func.count()).select_from(Message)) or 0)
            events = int(await session.scalar(select(func.count()).select_from(WebhookEvent)) or 0)
            return messages, events

    assert asyncio.run(counts()) == (0, 0)


def test_worker_processes_durable_inbound_sends_ai_reply_and_marks_event(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)
    callback_path = created["settings"]["callback_url"].removeprefix("https://api.example.test")
    assert client.post(callback_path, json=callback_payload()).status_code == 200

    async def run_worker() -> dict[str, int]:
        async with session_factory() as session:
            from app.services.channels.vk import process_pending_vk

            return await process_pending_vk(session)

    result = asyncio.run(run_worker())
    assert result == {"processed": 1}
    sends = [request for request in requests if request.url.path.endswith("/messages.send")]
    assert len(sends) == 1
    sent_form = parse_qs(sends[0].content.decode())
    assert sent_form["peer_id"] == ["70001"]
    assert sent_form["v"] == ["5.131"]
    assert int(sent_form["random_id"][0]) > 0

    async def final_state() -> tuple[Message, WebhookEvent]:
        async with session_factory() as session:
            outbound = (
                await session.execute(
                    select(Message).where(
                        Message.direction == "outbound",
                        Message.sender_type == "ai",
                    )
                )
            ).scalar_one()
            event = (await session.execute(select(WebhookEvent))).scalar_one()
            return outbound, event

    outbound, event = asyncio.run(final_state())
    assert outbound.external_message_id == "7001"
    assert outbound.status == "sent"
    assert event.processed is True
    assert event.processing_started_at is None
    assert asyncio.run(run_worker()) == {"processed": 0}
    assert len([request for request in requests if request.url.path.endswith("messages.send")]) == 1


def test_parallel_workers_claim_one_callback_only_once(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)
    callback_path = created["settings"]["callback_url"].removeprefix("https://api.example.test")
    assert client.post(callback_path, json=callback_payload(message_id=950)).status_code == 200

    async def run_parallel() -> list[dict[str, int]]:
        from app.services.channels.vk import process_pending_vk

        async def worker() -> dict[str, int]:
            async with session_factory() as session:
                return await process_pending_vk(session)

        return await asyncio.gather(worker(), worker())

    results = asyncio.run(run_parallel())
    assert sum(item["processed"] for item in results) == 1
    assert len([request for request in requests if request.url.path.endswith("messages.send")]) == 1


def test_worker_retries_existing_pending_outbound_with_original_text_and_random_id(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)
    callback_path = created["settings"]["callback_url"].removeprefix("https://api.example.test")
    assert client.post(callback_path, json=callback_payload(message_id=960)).status_code == 200

    async def seed_pending() -> uuid.UUID:
        async with session_factory() as session:
            inbound = (
                await session.execute(
                    select(Message).where(Message.external_message_id == "message:960")
                )
            ).scalar_one()
            outbound = Message(
                tenant_id=TENANT_ID,
                conversation_id=inbound.conversation_id,
                direction="outbound",
                sender_type="ai",
                text="Original durable answer",
                external_message_id=f"ai:{inbound.id}",
                status="pending",
                ai_meta={
                    "idempotency_key": f"ai:{inbound.id}",
                    "chat_id": "70001",
                    "delivery": "delivery-pending",
                },
            )
            session.add(outbound)
            await session.commit()
            return outbound.id

    outbound_id = asyncio.run(seed_pending())

    class LLMMustNotRun:
        provider_name = "must-not-run"

        async def generate_with_usage(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("LLM must not run when retrying a durable VK outbound")

    monkeypatch.setattr(
        "app.services.channels.telegram.get_llm",
        lambda _provider: LLMMustNotRun(),
    )

    async def run_worker() -> dict[str, int]:
        async with session_factory() as session:
            from app.services.channels.vk import process_pending_vk

            return await process_pending_vk(session)

    assert asyncio.run(run_worker()) == {"processed": 1}
    sends = [request for request in requests if request.url.path.endswith("messages.send")]
    assert len(sends) == 1
    form = parse_qs(sends[0].content.decode())
    assert form["message"] == ["Original durable answer"]

    from app.services.channels.vk import _stable_random_id

    assert form["random_id"] == [str(_stable_random_id(str(outbound_id)))]


def test_vk_delivery_retries_error_6_with_the_same_nonzero_random_id(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, _requests = connect_vk(client, monkeypatch)
    monkeypatch.undo()
    requests = install_vk_transport(
        monkeypatch,
        send_responses=[
            {"error": {"error_code": 6, "error_msg": "Too many requests"}},
            {"response": 8123},
        ],
    )

    async def send() -> object:
        async with session_factory() as session:
            channel = await session.get(Channel, uuid.UUID(created["id"]))
            assert channel is not None
            return await send_vk_message(
                channel,
                "70001",
                "Hello",
                idempotency_key="stable-idempotency-key",
            )

    result = asyncio.run(send())
    assert result.delivered is True
    assert result.external_message_id == "8123"
    sends = [request for request in requests if request.url.path.endswith("/messages.send")]
    assert len(sends) == 2
    forms = [parse_qs(request.content.decode()) for request in sends]
    assert forms[0]["random_id"] == forms[1]["random_id"]
    assert int(forms[0]["random_id"][0]) > 0
    assert all(form["v"] == ["5.131"] for form in forms)
    assert all(request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}" for request in sends)


def test_vk_delivery_surfaces_top_level_api_error(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, _requests = connect_vk(client, monkeypatch)
    monkeypatch.undo()
    install_vk_transport(
        monkeypatch,
        send_responses=[{"error": {"error_code": 15, "error_msg": "Access denied"}}],
    )

    async def send() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, uuid.UUID(created["id"]))
            assert channel is not None
            await send_vk_message(channel, "70001", "Hello", idempotency_key="error-case")

    with pytest.raises(Exception) as exc_info:
        asyncio.run(send())
    assert getattr(exc_info.value, "status_code", None) == 502


def test_manager_reply_uses_vk_messages_send(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)

    async def seed_conversation() -> uuid.UUID:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Anna")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=uuid.UUID(created["id"]),
                external_conversation_id="70001",
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
                    external_message_id="message:990",
                    status="received",
                    ai_meta={"source": "vk", "chat_id": "70001"},
                )
            )
            await session.commit()
            return conversation.id

    conversation_id = asyncio.run(seed_conversation())
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/reply",
        headers=auth_headers(),
        json={"text": "Manual answer"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"]["status"] == "sent"
    sends = [request for request in requests if request.url.path.endswith("/messages.send")]
    assert len(sends) == 1
    form = parse_qs(sends[0].content.decode())
    assert form["peer_id"] == ["70001"]
    assert form["message"] == ["Manual answer"]
    assert int(form["random_id"][0]) > 0

    async def stored_provider_id() -> str | None:
        async with session_factory() as session:
            result = await session.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.sender_type == "manager",
                )
            )
            return result.scalar_one().external_message_id

    assert asyncio.run(stored_provider_id()) == "7001"


def test_manager_vk_provider_failure_keeps_durable_pending_message(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, _requests = connect_vk(client, monkeypatch)

    async def seed_conversation() -> uuid.UUID:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Anna")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=uuid.UUID(created["id"]),
                external_conversation_id="70001",
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
                    external_message_id="message:991",
                    status="received",
                    ai_meta={"source": "vk", "chat_id": "70001"},
                )
            )
            await session.commit()
            return conversation.id

    conversation_id = asyncio.run(seed_conversation())
    monkeypatch.undo()
    install_vk_transport(
        monkeypatch,
        send_responses=[{"error": {"error_code": 15, "error_msg": "Access denied"}}],
    )
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/reply",
        headers=auth_headers(),
        json={"text": "Durable manager answer"},
    )
    assert response.status_code == 502

    async def stored() -> Message:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.sender_type == "manager",
                    )
                )
            ).scalar_one()

    message = asyncio.run(stored())
    assert message.status == "pending"
    assert message.text == "Durable manager answer"
    assert message.ai_meta["delivery"] == "vk-delivery-unknown"


def test_vk_rejects_text_over_9000_chars_without_provider_call(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_tenant(session_factory))
    created, requests = connect_vk(client, monkeypatch)

    async def seed_conversation() -> uuid.UUID:
        async with session_factory() as session:
            customer = Customer(tenant_id=TENANT_ID, display_name="Anna")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=TENANT_ID,
                customer_id=customer.id,
                channel_id=uuid.UUID(created["id"]),
                external_conversation_id="70001",
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
                    external_message_id="message:992",
                    status="received",
                    ai_meta={"source": "vk", "chat_id": "70001"},
                )
            )
            await session.commit()
            return conversation.id

    conversation_id = asyncio.run(seed_conversation())
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/reply",
        headers=auth_headers(),
        json={"text": "x" * 9001},
    )
    # The generic request schema rejects before the VK-specific 9000 limit;
    # direct delivery still enforces the official provider ceiling.
    assert response.status_code == 422
    assert len([request for request in requests if request.url.path.endswith("messages.send")]) == 0
