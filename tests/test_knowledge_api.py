"""Knowledge API tests: documents and candidate approval."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.security import create_token, hash_password
from app.db.session import get_session
from app.main import app
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.tenant import Tenant
from app.models.user import User

TENANT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222001")
CHANNEL_ID = uuid.UUID("22222222-2222-4222-8222-222222222010")
CUSTOMER_ID = uuid.UUID("22222222-2222-4222-8222-222222222020")
CONVERSATION_ID = uuid.UUID("22222222-2222-4222-8222-222222222030")
CANDIDATE_ID = uuid.UUID("22222222-2222-4222-8222-222222222040")


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
            Channel.__table__,
            Customer.__table__,
            Conversation.__table__,
            KbDocument.__table__,
            KbChunk.__table__,
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


async def seed_tenant(session_factory: async_sessionmaker[AsyncSession]) -> None:
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
        await session.commit()


async def seed_candidate(session_factory: async_sessionmaker[AsyncSession]) -> None:
    await seed_tenant(session_factory)
    async with session_factory() as session:
        session.add(
            Channel(
                id=CHANNEL_ID,
                tenant_id=TENANT_ID,
                type="telegram",
                name="Telegram",
                status="active",
                credentials_encrypted="",
                settings={},
            )
        )
        session.add(
            Customer(
                id=CUSTOMER_ID,
                tenant_id=TENANT_ID,
                display_name="Customer",
                note="",
            )
        )
        session.add(
            Conversation(
                id=CONVERSATION_ID,
                tenant_id=TENANT_ID,
                customer_id=CUSTOMER_ID,
                channel_id=CHANNEL_ID,
                status="open",
                assignee_user_id=USER_ID,
                last_message_at=datetime.now(UTC),
                last_message_preview="Можно ли подключить Telegram?",
                unread_count=1,
            )
        )
        session.add(
            KbCandidate(
                id=CANDIDATE_ID,
                tenant_id=TENANT_ID,
                conversation_id=CONVERSATION_ID,
                question="Можно ли подключить Telegram?",
                answer="Да, Telegram подключается через токен бота.",
                suggested_by="manager",
                status="pending",
                resulting_document_id=None,
            )
        )
        await session.commit()


def test_create_and_list_knowledge_documents(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))

    created = client.post(
        "/api/v1/knowledge/documents",
        headers=auth_headers(),
        json={
            "title": "FAQ Telegram",
            "source_type": "manual",
            "text": (
                "Telegram подключается через токен бота.\n\n"
                "После подключения можно синхронизировать чаты."
            ),
            "tags": {"topic": "telegram"},
        },
    )

    assert created.status_code == 200, created.text
    created_data = created.json()
    assert created_data["title"] == "FAQ Telegram"
    assert created_data["status"] == "ready"
    assert created_data["chunks_count"] == 1

    listed = client.get("/api/v1/knowledge/documents", headers=auth_headers())

    assert listed.status_code == 200
    documents = listed.json()
    assert len(documents) == 1
    assert documents[0]["id"] == created_data["id"]
    assert documents[0]["chunks_count"] == 1

    detail = client.get(
        f"/api/v1/knowledge/documents/{created_data['id']}",
        headers=auth_headers(),
    )

    assert detail.status_code == 200, detail.text
    detail_data = detail.json()
    assert detail_data["id"] == created_data["id"]
    assert len(detail_data["chunks"]) == 1
    assert detail_data["chunks"][0]["position"] == 0

    archived = client.post(
        f"/api/v1/knowledge/documents/{created_data['id']}/archive",
        headers=auth_headers(),
    )

    assert archived.status_code == 200, archived.text
    assert archived.json()["document"]["status"] == "archived"


def test_list_and_approve_knowledge_candidate(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_candidate(session_factory))

    listed = client.get("/api/v1/knowledge/candidates", headers=auth_headers())

    assert listed.status_code == 200
    candidates = listed.json()
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending"

    approved = client.post(
        f"/api/v1/knowledge/candidates/{CANDIDATE_ID}/approve",
        headers=auth_headers(),
    )

    assert approved.status_code == 200, approved.text
    data = approved.json()
    assert data["status"] == "approved"
    assert data["resulting_document_id"]
    assert data["document"]["title"].startswith("Ответ из диалога:")
    assert data["document"]["chunks_count"] == 1


def test_reject_knowledge_candidate(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_candidate(session_factory))

    rejected = client.post(
        f"/api/v1/knowledge/candidates/{CANDIDATE_ID}/reject",
        headers=auth_headers(),
    )

    assert rejected.status_code == 200, rejected.text
    data = rejected.json()
    assert data["status"] == "rejected"
    assert data["resulting_document_id"] is None
