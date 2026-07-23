"""Telegram personal-account auth flow tests without live Telegram calls."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace
from typing import cast

import pytest
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
        for table in (Tenant.__table__, User.__table__, Channel.__table__):
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
