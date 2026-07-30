"""Telegram personal-account auth flow tests without live Telegram calls."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.core.security import create_token
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.tenant import Tenant
from app.models.user import User

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1")
USER_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2")


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
            Channel.__table__,
            Customer.__table__,
            CustomerIdentity.__table__,
            Conversation.__table__,
            Message.__table__,
        ):
            await conn.run_sync(lambda connection, item=table: cast(Table, item).create(connection))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Test", slug="mtproto", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="owner@example.com",
                full_name="Owner",
                role="owner",
                password_hash="hash",
                status="active",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture()
def client(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    monkeypatch.setattr(settings, "TELEGRAM_API_ID", 12345)
    monkeypatch.setattr(settings, "TELEGRAM_API_HASH", "application-hash")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(USER_ID, tenant_id=TENANT_ID, role='owner')}"}


def test_personal_account_otp_flow(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def save(self) -> str:
            return "serialized-session"

    class FakeClient:
        session = FakeSession()

        def __init__(self, *_args: object) -> None: ...

        async def connect(self) -> None: ...

        async def disconnect(self) -> None: ...

        async def send_code_request(self, phone: str) -> object:
            assert phone == "+79990001122"
            return SimpleNamespace(phone_code_hash="code-hash")

        async def sign_in(self, **kwargs: object) -> None:
            assert kwargs["code"] == "12345"

        async def get_me(self) -> object:
            return SimpleNamespace(id=77, first_name="Тимур", last_name="", username="timur")

    monkeypatch.setattr(
        "app.services.channels.telegram_mtproto.TelegramClient",
        FakeClient,
    )

    started = client.post(
        "/api/v1/channels/telegram/account/start",
        headers=headers(),
        json={"phone": "+79990001122"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "code_required"

    confirmed = client.post(
        "/api/v1/channels/telegram/account/confirm",
        headers=headers(),
        json={"channel_id": started.json()["channel_id"], "code": "12345"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "active"
    assert confirmed.json()["display_name"] == "Тимур"

    async def stored_channel() -> Channel:
        async with session_factory() as session:
            result = await session.execute(select(Channel))
            return result.scalar_one()

    channel = asyncio.run(stored_channel())
    assert channel.status == "active"
    assert channel.settings["transport"] == "mtproto"
    assert channel.settings["phone_masked"] == "***1122"
    assert channel.credentials_encrypted.startswith("fernet:")
    assert "serialized-session" not in channel.credentials_encrypted


def test_personal_account_requires_application_credentials(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_API_ID", 0)
    response = client.post(
        "/api/v1/channels/telegram/account/start",
        headers=headers(),
        json={"phone": "+79990001122"},
    )
    assert response.status_code == 503


def test_mtproto_delivery_uses_access_hash_and_returns_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.channels import telegram_mtproto

    sent_to: list[object] = []

    class FakeClient:
        async def iter_dialogs(self):
            yield SimpleNamespace(
                id=6154961834,
                dialog=SimpleNamespace(read_outbox_max_id=4242),
            )

        async def send_message(self, peer: object, text: str) -> object:
            sent_to.append(peer)
            assert text == "Ответ"
            return SimpleNamespace(id=4242)

        async def disconnect(self) -> None: ...

    async def fake_authorized_client(_channel: Channel) -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(telegram_mtproto, "create_authorized_client", fake_authorized_client)
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        type="telegram",
        name="Telegram",
        status="active",
        credentials_encrypted="encrypted",
        settings={"transport": "mtproto"},
    )

    result = asyncio.run(
        telegram_mtproto.send_mtproto_message(
            channel,
            "6154961834",
            "Ответ",
            peer_access_hash=987654321,
        )
    )

    assert result.delivered is True
    assert result.message_id == 4242
    assert result.read is True
    assert sent_to[0].user_id == 6154961834
    assert sent_to[0].access_hash == 987654321


def test_mtproto_delivery_refreshes_dialogs_for_legacy_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.channels import telegram_mtproto

    legacy_peer = object()

    class FakeClient:
        async def get_input_entity(self, _peer_id: int) -> object:
            raise ValueError("entity cache is empty")

        async def iter_dialogs(self):
            yield SimpleNamespace(
                id=6154961834,
                input_entity=legacy_peer,
                dialog=SimpleNamespace(read_outbox_max_id=499),
            )

        async def send_message(self, peer: object, _text: str) -> object:
            assert peer is legacy_peer
            return SimpleNamespace(id=500)

        async def disconnect(self) -> None: ...

    async def fake_authorized_client(_channel: Channel) -> FakeClient:
        return FakeClient()

    monkeypatch.setattr(telegram_mtproto, "create_authorized_client", fake_authorized_client)
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        type="telegram",
        name="Telegram",
        status="active",
        credentials_encrypted="encrypted",
        settings={"transport": "mtproto"},
    )

    result = asyncio.run(telegram_mtproto.send_mtproto_message(channel, "6154961834", "Ответ"))

    assert result.delivered is True
    assert result.message_id == 500
    assert result.read is False


def test_mtproto_client_setup_failure_becomes_bad_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.channels import telegram_mtproto

    async def fail_authorization(_channel: Channel) -> object:
        raise RuntimeError("session is no longer authorized")

    monkeypatch.setattr(telegram_mtproto, "create_authorized_client", fail_authorization)
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=TENANT_ID,
        type="telegram",
        name="Telegram",
        status="active",
        credentials_encrypted="encrypted",
        settings={"transport": "mtproto"},
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(telegram_mtproto.send_mtproto_message(channel, "7001", "Ответ"))

    assert error.value.status_code == 502


def test_existing_customer_avatar_backfill_is_tenant_scoped(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.channels.telegram_mtproto import sync_mtproto_customer_avatars
    from app.services.customer_avatars import get_customer_avatar

    monkeypatch.setattr(settings, "CUSTOMER_AVATAR_DIR", str(tmp_path))
    channel_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    jpeg = b"\xff\xd8\xff" + b"telegram-avatar"

    class FakeClient:
        async def iter_dialogs(self):
            yield SimpleNamespace(id=7001, entity=SimpleNamespace(photo=object()))
            yield SimpleNamespace(id=9999, entity=SimpleNamespace(photo=object()))

        async def download_profile_photo(self, entity: object, *, file: object) -> bytes:
            assert file is bytes
            return jpeg

    async def seed_and_sync() -> int:
        async with session_factory() as session:
            channel = Channel(
                id=channel_id,
                tenant_id=TENANT_ID,
                type="telegram",
                name="Telegram",
                status="active",
                credentials_encrypted="encrypted",
                settings={"transport": "mtproto"},
            )
            session.add(channel)
            session.add(Customer(id=customer_id, tenant_id=TENANT_ID, display_name="Client"))
            session.add(
                CustomerIdentity(
                    customer_id=customer_id,
                    channel_id=channel_id,
                    external_user_id="7001",
                )
            )
            await session.commit()
            return await sync_mtproto_customer_avatars(session, channel, FakeClient())  # type: ignore[arg-type]

    assert asyncio.run(seed_and_sync()) == 1
    stored = get_customer_avatar(TENANT_ID, customer_id)
    assert stored is not None
    assert Path(stored[0]).read_bytes() == jpeg


def test_mtproto_read_receipt_marks_sent_outbound_messages_read(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.channels.telegram_mtproto import mark_mtproto_messages_read

    channel_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    async def seed_and_mark() -> tuple[int, dict[int, str]]:
        async with session_factory() as session:
            session.add(
                Channel(
                    id=channel_id,
                    tenant_id=TENANT_ID,
                    type="telegram",
                    name="Telegram",
                    status="active",
                    credentials_encrypted="encrypted",
                    settings={"transport": "mtproto"},
                )
            )
            session.add(Customer(id=customer_id, tenant_id=TENANT_ID, display_name="Клиент"))
            session.add(
                Conversation(
                    id=conversation_id,
                    tenant_id=TENANT_ID,
                    customer_id=customer_id,
                    channel_id=channel_id,
                    status="answered",
                    unread_count=0,
                )
            )
            for telegram_id in (41, 43):
                session.add(
                    Message(
                        tenant_id=TENANT_ID,
                        conversation_id=conversation_id,
                        direction="outbound",
                        sender_type="manager",
                        text=f"Ответ {telegram_id}",
                        status="sent",
                        ai_meta={"chat_id": "7001", "telegram_message_id": telegram_id},
                    )
                )
            await session.commit()
            changed = await mark_mtproto_messages_read(
                session,
                channel_id,
                peer_id=7001,
                max_message_id=41,
            )
            result = await session.execute(select(Message))
            return changed, {
                int(message.ai_meta["telegram_message_id"]): message.status
                for message in result.scalars().all()
            }

    changed, statuses = asyncio.run(seed_and_mark())
    assert changed == 1
    assert statuses == {41: "read", 43: "sent"}


def test_read_watermark_closes_receipt_before_message_commit_race(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.channels.telegram_mtproto import (
        apply_mtproto_read_watermark,
        mark_mtproto_messages_read,
    )

    channel_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    async def seed_and_apply() -> tuple[int, str]:
        async with session_factory() as session:
            channel = Channel(
                id=channel_id,
                tenant_id=TENANT_ID,
                type="telegram",
                name="Telegram",
                status="active",
                credentials_encrypted="encrypted",
                settings={"transport": "mtproto"},
            )
            session.add(channel)
            session.add(Customer(id=customer_id, tenant_id=TENANT_ID, display_name="Клиент"))
            session.add(
                Conversation(
                    id=conversation_id,
                    tenant_id=TENANT_ID,
                    customer_id=customer_id,
                    channel_id=channel_id,
                    status="answered",
                    unread_count=0,
                )
            )
            await session.commit()
            changed = await mark_mtproto_messages_read(
                session,
                channel_id,
                peer_id=7001,
                max_message_id=52,
            )
            message = Message(
                tenant_id=TENANT_ID,
                conversation_id=conversation_id,
                direction="outbound",
                sender_type="manager",
                text="Уже прочитано",
                status="sent",
                ai_meta={"chat_id": "7001", "telegram_message_id": 52},
            )
            session.add(message)
            await session.commit()
            applied = await apply_mtproto_read_watermark(session, channel_id, message)
            return changed, message.status if applied else "not-applied"

    changed, final_status = asyncio.run(seed_and_apply())
    assert changed == 0
    assert final_status == "read"


def test_read_receipt_reconciliation_recovers_missed_telegram_update(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.channels import telegram_mtproto

    channel_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    conversation_id = uuid.uuid4()

    class FakeClient:
        async def __call__(self, request: object) -> SimpleNamespace:
            assert request.__class__.__name__ == "GetPeerDialogsRequest"
            return SimpleNamespace(
                dialogs=[
                    SimpleNamespace(
                        peer=SimpleNamespace(user_id=7001),
                        read_outbox_max_id=61,
                    )
                ]
            )

    async def fake_resolve_peer(
        _client: object,
        peer_id: int,
        _access_hash: int | None,
    ) -> object:
        return SimpleNamespace(user_id=peer_id)

    def fake_get_peer_id(peer: object) -> int:
        return int(cast(SimpleNamespace, peer).user_id)

    monkeypatch.setattr(telegram_mtproto, "_resolve_peer", fake_resolve_peer)
    monkeypatch.setattr(telegram_mtproto.telegram_utils, "get_peer_id", fake_get_peer_id)

    async def seed_and_reconcile() -> tuple[int, dict[int, str], dict[str, int]]:
        async with session_factory() as session:
            session.add(
                Channel(
                    id=channel_id,
                    tenant_id=TENANT_ID,
                    type="telegram",
                    name="Telegram",
                    status="active",
                    credentials_encrypted="encrypted",
                    settings={"transport": "mtproto"},
                )
            )
            session.add(Customer(id=customer_id, tenant_id=TENANT_ID, display_name="Клиент"))
            session.add(
                Conversation(
                    id=conversation_id,
                    tenant_id=TENANT_ID,
                    customer_id=customer_id,
                    channel_id=channel_id,
                    status="answered",
                    unread_count=0,
                )
            )
            for telegram_id in (60, 62):
                session.add(
                    Message(
                        tenant_id=TENANT_ID,
                        conversation_id=conversation_id,
                        direction="outbound",
                        sender_type="manager",
                        text=f"Ответ {telegram_id}",
                        status="sent",
                        ai_meta={
                            "chat_id": "7001",
                            "peer_access_hash": 123,
                            "telegram_message_id": telegram_id,
                        },
                    )
                )
            await session.commit()
            changed = await telegram_mtproto.reconcile_mtproto_messages_read(
                session,
                channel_id,
                FakeClient(),  # type: ignore[arg-type]
            )
            result = await session.execute(select(Message))
            channel = await session.get(Channel, channel_id)
            assert channel is not None
            return (
                changed,
                {
                    int(message.ai_meta["telegram_message_id"]): message.status
                    for message in result.scalars().all()
                },
                channel.settings["read_watermarks"],
            )

    changed, statuses, watermarks = asyncio.run(seed_and_reconcile())
    assert changed == 1
    assert statuses == {60: "read", 62: "sent"}
    assert watermarks == {"7001": 61}
