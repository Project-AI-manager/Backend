"""Conversation actions API tests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
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


def test_manager_reply_via_mtproto_persists_telegram_delivery_metadata(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_conversation(session_factory))

    async def enable_mtproto() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, CHANNEL_ID)
            assert channel is not None
            channel.settings = {**channel.settings, "transport": "mtproto"}
            inbound_result = await session.execute(
                select(Message).where(Message.direction == "inbound")
            )
            inbound = inbound_result.scalar_one()
            inbound.ai_meta = {**inbound.ai_meta, "peer_access_hash": 987654321}
            await session.commit()

    calls: list[tuple[str, str, int | None]] = []

    async def fake_send(
        _channel: Channel,
        peer_id: str,
        text: str,
        *,
        peer_access_hash: int | None = None,
    ) -> object:
        calls.append((peer_id, text, peer_access_hash))
        return type(
            "Delivery",
            (),
            {"delivered": True, "message_id": 4242, "read": True},
        )()

    asyncio.run(enable_mtproto())
    monkeypatch.setattr("app.services.conversations.send_mtproto_message", fake_send)

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply",
        headers=auth_headers(),
        json={"text": "Ответ через Telegram."},
    )

    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert calls == [("7001", "Ответ через Telegram.", 987654321)]
    assert message["status"] == "read"
    assert message["ai_meta"]["telegram_message_id"] == 4242
    assert message["ai_meta"]["delivery"] == "channel-sent"


def test_manager_reply_marks_definitive_mtproto_failure(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(seed_conversation(session_factory))

    async def enable_mtproto() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, CHANNEL_ID)
            assert channel is not None
            channel.settings = {**channel.settings, "transport": "mtproto"}
            await session.commit()

    async def fake_send(*_args: object, **_kwargs: object) -> object:
        return type("Delivery", (), {"delivered": False, "message_id": None})()

    asyncio.run(enable_mtproto())
    monkeypatch.setattr("app.services.conversations.send_mtproto_message", fake_send)

    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply",
        headers=auth_headers(),
        json={"text": "Не доставится."},
    )

    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert message["status"] == "failed"
    assert message["ai_meta"]["delivery"] == "telegram-mtproto-failed"


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


def test_conversation_avatar_is_private_and_exposed_by_url(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.services.customer_avatars import store_customer_avatar

    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CUSTOMER_AVATAR_DIR", str(tmp_path))
    jpeg = b"\xff\xd8\xff" + b"avatar"
    assert store_customer_avatar(TENANT_ID, CUSTOMER_ID, jpeg)

    listed = client.get("/api/v1/conversations", headers=auth_headers())
    assert listed.status_code == 200
    avatar_url = listed.json()[0]["avatar_url"]
    assert avatar_url == f"/api/v1/conversations/{CONVERSATION_ID}/avatar"

    unauthorized = client.get(avatar_url)
    assert unauthorized.status_code in {401, 403}
    downloaded = client.get(avatar_url, headers=auth_headers())
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "image/jpeg"
    assert downloaded.headers["cache-control"] == "private, max-age=300"
    assert downloaded.content == jpeg


def test_conversation_without_avatar_has_no_avatar_url(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CUSTOMER_AVATAR_DIR", str(tmp_path))
    response = client.get("/api/v1/conversations", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()[0]["avatar_url"] is None


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


def test_mtproto_photo_attachment_upload_send_and_download(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))

    async def enable_mtproto() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, CHANNEL_ID)
            assert channel is not None
            channel.settings = {**channel.settings, "transport": "mtproto"}
            await session.commit()

    calls: list[tuple[str, str, bool]] = []

    async def fake_send_file(
        _channel: Channel,
        peer_id: str,
        file_path: str,
        caption: str,
        **kwargs: object,
    ) -> object:
        calls.append((peer_id, caption, bool(kwargs["force_document"])))
        assert Path(file_path).read_bytes().startswith(b"\x89PNG")
        return type("Delivery", (), {"delivered": True, "message_id": 77})()

    asyncio.run(enable_mtproto())
    monkeypatch.setattr("app.services.conversations.send_mtproto_file", fake_send_file)
    image = b"\x89PNG\r\n\x1a\n" + b"png-data"
    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        data={"text": "Фото"},
        files={"file": ("../photo.png", image, "image/png")},
    )

    assert response.status_code == 200, response.text
    message = response.json()["message"]
    item = message["attachments"]["items"][0]
    assert calls == [("7001", "Фото", False)]
    assert item["name"] == "photo.png"
    assert item["size_bytes"] == len(image)
    assert item["telegram_message_id"] == 77
    assert "storage_key" not in item
    assert item["download_url"].endswith(f"/attachments/{item['id']}")
    assert message["status"] == "sent"

    downloaded = client.get(
        f"/api/v1/conversations/{CONVERSATION_ID}/attachments/{item['id']}",
        headers=auth_headers(),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == image
    assert downloaded.headers["x-content-type-options"] == "nosniff"

    other_tenant = uuid.uuid4()
    other_user = uuid.uuid4()

    async def seed_other_tenant() -> None:
        async with session_factory() as session:
            session.add(Tenant(id=other_tenant, name="Other", slug="other", status="active"))
            session.add(
                User(
                    id=other_user,
                    tenant_id=other_tenant,
                    email="other@example.com",
                    full_name="Other",
                    role="owner",
                    password_hash=hash_password("other-password"),
                    status="active",
                )
            )
            await session.commit()

    asyncio.run(seed_other_tenant())
    other_headers = {
        "Authorization": (
            f"Bearer {create_token(other_user, tenant_id=other_tenant, role='owner')}"
        )
    }
    forbidden = client.get(
        f"/api/v1/conversations/{CONVERSATION_ID}/attachments/{item['id']}",
        headers=other_headers,
    )
    assert forbidden.status_code == 404


def test_mtproto_document_attachment_uses_document_mode(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))

    async def enable_mtproto() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, CHANNEL_ID)
            assert channel is not None
            channel.settings = {**channel.settings, "transport": "mtproto"}
            await session.commit()

    document_mode: list[bool] = []

    async def fake_send_file(*_args: object, **kwargs: object) -> object:
        document_mode.append(bool(kwargs["force_document"]))
        return type("Delivery", (), {"delivered": True, "message_id": 78})()

    asyncio.run(enable_mtproto())
    monkeypatch.setattr("app.services.conversations.send_mtproto_file", fake_send_file)
    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        files={"file": ("terms.pdf", b"%PDF-1.7\nbody", "application/pdf")},
    )
    assert response.status_code == 200, response.text
    assert document_mode == [True]


def test_mtproto_multiple_attachments_keep_order_and_send_caption_once(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))

    async def enable_mtproto() -> None:
        async with session_factory() as session:
            channel = await session.get(Channel, CHANNEL_ID)
            assert channel is not None
            channel.settings = {**channel.settings, "transport": "mtproto"}
            await session.commit()

    calls: list[tuple[str, str]] = []

    async def fake_send_file(
        _channel: Channel,
        _peer_id: str,
        file_path: str,
        caption: str,
        **_kwargs: object,
    ) -> object:
        calls.append((Path(file_path).name, caption))
        return type(
            "Delivery",
            (),
            {"delivered": True, "message_id": 100 + len(calls), "read": False},
        )()

    asyncio.run(enable_mtproto())
    monkeypatch.setattr("app.services.conversations.send_mtproto_file", fake_send_file)
    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        data={"text": "Документы по порядку"},
        files=[
            ("files", ("first.txt", b"first", "text/plain")),
            ("files", ("second.pdf", b"%PDF-1.7\nsecond", "application/pdf")),
        ],
    )

    assert response.status_code == 200, response.text
    message = response.json()["message"]
    items = message["attachments"]["items"]
    assert [item["name"] for item in items] == ["first.txt", "second.pdf"]
    assert [item["telegram_message_id"] for item in items] == [101, 102]
    assert calls == [
        (Path(items[0]["download_url"]).name + ".txt", "Документы по порядку"),
        (Path(items[1]["download_url"]).name + ".pdf", ""),
    ]
    assert message["text"] == "Документы по порядку"
    assert message["status"] == "sent"


def test_bot_api_attachment_selects_photo_and_persists_message_id(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))
    calls: list[tuple[str, bool]] = []

    async def fake_bot_file(
        _channel: Channel,
        _chat_id: str,
        _file_path: str,
        caption: str,
        *,
        is_image: bool,
    ) -> tuple[bool, int]:
        calls.append((caption, is_image))
        return True, 79

    monkeypatch.setattr("app.services.conversations.send_telegram_file", fake_bot_file)
    image = b"\xff\xd8\xff" + b"jpeg-data"
    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        data={"text": "Фото bot"},
        files={"file": ("photo.jpg", image, "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    assert calls == [("Фото bot", True)]
    assert response.json()["message"]["ai_meta"]["telegram_message_id"] == 79


def test_attachment_validation_size_type_and_signature(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))
    oversized = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        files={"file": ("large.txt", b"x" * (10 * 1024 * 1024 + 1), "text/plain")},
    )
    unsupported = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        files={"file": ("script.exe", b"MZ", "application/octet-stream")},
    )
    mismatch = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        files={"file": ("fake.pdf", b"not-pdf", "application/pdf")},
    )
    long_caption = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        data={"text": "x" * 1025},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert oversized.status_code == 413
    assert unsupported.status_code == 415
    assert mismatch.status_code == 415
    assert long_caption.status_code == 422


def test_attachment_download_is_tenant_scoped_and_closed_chat_rejected(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))
    closed = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/close", headers=auth_headers()
    )
    assert closed.status_code == 200
    response = client.post(
        f"/api/v1/conversations/{CONVERSATION_ID}/reply-with-file",
        headers=auth_headers(),
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 409
    assert not list(tmp_path.rglob("*.*"))

    missing = client.get(
        f"/api/v1/conversations/{uuid.uuid4()}/attachments/{uuid.uuid4()}",
        headers=auth_headers(),
    )
    assert missing.status_code == 404


def test_attachment_upload_unknown_conversation_cleans_local_file(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    asyncio.run(seed_conversation(session_factory))
    monkeypatch.setattr(settings, "CONVERSATION_UPLOAD_DIR", str(tmp_path))
    response = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/reply-with-file",
        headers=auth_headers(),
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 404
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
