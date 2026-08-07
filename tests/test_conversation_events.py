"""Tenant isolation for the inbox SSE watermark."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import create_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, Message
from app.models.tenant import Tenant
from app.models.user import User
from app.services.conversation_events import conversation_event_signature


@pytest.mark.asyncio
async def test_signature_is_tenant_scoped_and_changes_with_own_messages() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        for table in (
            Tenant.__table__,
            Channel.__table__,
            Customer.__table__,
            Conversation.__table__,
            Message.__table__,
        ):
            await connection.run_sync(table.create)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with factory() as session:
        session.add_all(
            [
                Tenant(id=tenant_a, name="A", slug="a", status="active"),
                Tenant(id=tenant_b, name="B", slug="b", status="active"),
            ]
        )
        for tenant_id, suffix in ((tenant_a, "a"), (tenant_b, "b")):
            channel = Channel(
                tenant_id=tenant_id,
                type="telegram",
                name=suffix,
                status="active",
                credentials_encrypted="",
                settings={},
            )
            customer = Customer(tenant_id=tenant_id, display_name=suffix, note="")
            session.add_all([channel, customer])
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    channel_id=channel.id,
                    status="open",
                    last_message_at=datetime.now(UTC),
                    last_message_preview="",
                    unread_count=0,
                )
            )
        await session.commit()

    async with factory() as session:
        initial_a = await conversation_event_signature(session, tenant_a)
        initial_b = await conversation_event_signature(session, tenant_b)
        conversation_a = (
            await session.execute(
                select(Conversation).where(Conversation.tenant_id == tenant_a)
            )
        ).scalar_one()
        session.add(
            Message(
                tenant_id=tenant_a,
                conversation_id=conversation_a.id,
                direction="inbound",
                sender_type="customer",
                text="new",
                attachments={},
                status="received",
                ai_meta={},
            )
        )
        await session.commit()

    async with factory() as session:
        assert await conversation_event_signature(session, tenant_a) != initial_a
        assert await conversation_event_signature(session, tenant_b) == initial_b
    await engine.dispose()


def test_events_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/conversations/events")
    assert response.status_code == 401


def test_events_rejects_tenant_claim_that_does_not_match_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def prepare() -> async_sessionmaker:
        async with engine.begin() as connection:
            for table in (Tenant.__table__, User.__table__):
                await connection.run_sync(table.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(Tenant(id=tenant_id, name="A", slug="tenant-a", status="active"))
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email="owner@example.com",
                    full_name="Owner",
                    role="owner",
                    password_hash=hash_password("demo-password"),
                    status="active",
                )
            )
            await session.commit()
        return factory

    import asyncio

    factory = asyncio.run(prepare())

    async def override_get_session() -> AsyncGenerator:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    monkeypatch.setattr("app.api.deps.SessionLocal", factory)
    token = create_token(user_id, tenant_id=other_tenant_id, role="owner")
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/conversations/events",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        asyncio.run(engine.dispose())
    assert response.status_code == 401
