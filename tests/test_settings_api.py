"""Settings API tests."""

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

from app.core.security import create_token
from app.db.session import get_session
from app.main import app
from app.models.ops import Plan, Subscription, UsageCounter
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import User

TENANT_ID = uuid.UUID("55555555-5555-4555-8555-555555555501")
USER_ID = uuid.UUID("55555555-5555-4555-8555-555555555502")


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
            TenantAIConfig.__table__,
            Plan.__table__,
            Subscription.__table__,
            UsageCounter.__table__,
        ):
            await conn.run_sync(create_table, table)

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


def auth_headers(role: str = "owner") -> dict[str, str]:
    token = create_token(USER_ID, tenant_id=TENANT_ID, role=role)
    return {"Authorization": f"Bearer {token}"}


async def seed_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    with_ai_config: bool = True,
    with_billing: bool = False,
    role: str = "owner",
) -> None:
    async with session_factory() as session:
        session.add(Tenant(id=TENANT_ID, name="ООО Север", slug="sever", status="active"))
        session.add(
            User(
                id=USER_ID,
                tenant_id=TENANT_ID,
                email="owner@example.com",
                full_name="Owner",
                role=role,
                password_hash="test-password-hash",
                status="active",
            )
        )
        if with_ai_config:
            session.add(
                TenantAIConfig(
                    tenant_id=TENANT_ID,
                    auto_reply_enabled=False,
                    confidence_threshold=80,
                    llm_provider="mock",
                    embedding_model="local",
                    system_prompt="",
                )
            )
        if with_billing:
            plan = Plan(
                code="demo",
                name="Demo",
                price_month=0,
                dialog_limit=500,
                channel_limit=1,
                features={"telegram": True},
            )
            session.add(plan)
            await session.flush()
            session.add(Subscription(tenant_id=TENANT_ID, plan_id=plan.id, status="trial"))
            session.add(
                UsageCounter(
                    tenant_id=TENANT_ID,
                    period=datetime.now(UTC).strftime("%Y-%m"),
                    dialogs_count=7,
                    ai_replies_count=3,
                )
            )
        await session.commit()


def test_get_ai_settings_creates_default_config(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, with_ai_config=False))

    response = client.get("/api/v1/settings/ai", headers=auth_headers())

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["auto_reply_enabled"] is False
    assert data["confidence_threshold"] == 80
    assert data["llm_provider"] == "mock"
    assert data["embedding_model"] == "multilingual-e5-large"
    assert "unirouter" in data["available_providers"]


def test_update_ai_settings_persists_config(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))

    response = client.put(
        "/api/v1/settings/ai",
        headers=auth_headers(),
        json={
            "auto_reply_enabled": True,
            "confidence_threshold": 55,
            "llm_provider": "unirouter",
            "embedding_model": "local",
            "system_prompt": "Отвечай только по базе знаний.",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["auto_reply_enabled"] is True
    assert data["confidence_threshold"] == 55
    assert data["llm_provider"] == "unirouter"
    assert data["system_prompt"] == "Отвечай только по базе знаний."


def test_update_ai_settings_rejects_unknown_provider(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))

    response = client.put(
        "/api/v1/settings/ai",
        headers=auth_headers(),
        json={"llm_provider": "unknown"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "unsupported_llm_provider"
    assert detail["message"] == detail["msg"]
    assert "mock" in detail["available_providers"]


def test_manager_can_read_but_cannot_update_sensitive_settings(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, role="manager"))
    headers = auth_headers(role="manager")

    read_response = client.get("/api/v1/settings/ai", headers=headers)
    ai_response = client.put(
        "/api/v1/settings/ai",
        headers=headers,
        json={"auto_reply_enabled": True},
    )
    workspace_response = client.put(
        "/api/v1/settings/workspace",
        headers=headers,
        json={"name": "Unauthorized rename"},
    )

    assert read_response.status_code == 200
    assert ai_response.status_code == 403
    assert workspace_response.status_code == 403


def test_workspace_settings_read_and_update_company_name(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory))

    initial = client.get("/api/v1/settings/workspace", headers=auth_headers())
    updated = client.put(
        "/api/v1/settings/workspace",
        headers=auth_headers(),
        json={"name": "ООО Юг"},
    )

    assert initial.status_code == 200, initial.text
    assert initial.json()["name"] == "ООО Север"
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "ООО Юг"
    assert updated.json()["slug"] == "sever"


def test_billing_settings_uses_subscription_and_current_usage(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asyncio.run(seed_tenant(session_factory, with_billing=True))

    response = client.get("/api/v1/settings/billing", headers=auth_headers())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "plan": "demo",
        "plan_name": "Demo",
        "subscription_status": "trial",
        "dialogs_used": 7,
        "dialogs_limit": 500,
        "ai_replies_used": 3,
        "channel_limit": 1,
    }
