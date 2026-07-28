"""User notification preferences API tests."""

import uuid
from collections.abc import AsyncGenerator, Generator
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
from app.models.tenant import Tenant
from app.models.user import User, UserNotificationSettings

TENANT_ID = uuid.UUID("56555555-5555-4555-8555-555555555501")
USER_ID = uuid.UUID("56555555-5555-4555-8555-555555555502")


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
        for table in (Tenant.__table__, User.__table__, UserNotificationSettings.__table__):
            await conn.run_sync(create_table, table)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add(Tenant(id=TENANT_ID, name="Autopilot", slug="autopilot", status="active"))
            session.add(
                User(
                    id=USER_ID,
                    tenant_id=TENANT_ID,
                    email="owner@example.com",
                    full_name="Owner",
                    role="owner",
                    password_hash="test-password-hash",
                    status="active",
                )
            )
            await session.commit()
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


def headers() -> dict[str, str]:
    token = create_token(USER_ID, tenant_id=TENANT_ID, role="owner")
    return {"Authorization": f"Bearer {token}"}


def test_notification_preferences_default_to_escalation_email(client: TestClient) -> None:
    response = client.get("/api/v1/users/me/notifications", headers=headers())

    assert response.status_code == 200, response.text
    assert response.json() == {
        "escalation_email_enabled": True,
        "daily_digest_email_enabled": False,
    }


def test_notification_preferences_are_persisted(client: TestClient) -> None:
    response = client.put(
        "/api/v1/users/me/notifications",
        headers=headers(),
        json={"escalation_email_enabled": False, "daily_digest_email_enabled": True},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "escalation_email_enabled": False,
        "daily_digest_email_enabled": True,
    }
