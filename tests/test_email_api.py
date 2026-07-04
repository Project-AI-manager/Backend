"""Email module API tests."""

from collections.abc import AsyncGenerator, Generator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.db.session import get_session
from app.main import app
from app.models.email import EmailOutbox, EmailToken
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import RefreshToken, User


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
            TenantAIConfig.__table__,
            User.__table__,
            RefreshToken.__table__,
            EmailToken.__table__,
            EmailOutbox.__table__,
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


def register(client: TestClient) -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "ООО Север",
            "email": "owner@example.com",
            "password": "demo-password",
            "full_name": "Тимур",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_email_status_is_public(client: TestClient) -> None:
    response = client.get("/api/v1/email/status")

    assert response.status_code == 200
    data = response.json()
    assert data["dev_mode"] is True
    assert data["smtp_configured"] is False


def test_email_verification_flow(client: TestClient) -> None:
    tokens = register(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    requested = client.post("/api/v1/email/verification/request", headers=headers)

    assert requested.status_code == 200, requested.text
    dev_token = requested.json()["dev_token"]
    assert dev_token

    outbox = client.get("/api/v1/email/outbox", headers=headers)
    assert outbox.status_code == 200
    assert outbox.json()[0]["purpose"] == "verify_email"
    assert outbox.json()[0]["status"] == "dev"

    confirmed = client.post("/api/v1/email/verification/confirm", json={"token": dev_token})
    assert confirmed.status_code == 200, confirmed.text

    me = client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email_verified"] is True


def test_password_reset_flow(client: TestClient) -> None:
    register(client)

    requested = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "owner@example.com"},
    )

    assert requested.status_code == 200, requested.text
    dev_token = requested.json()["dev_token"]
    assert dev_token

    confirmed = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": dev_token, "new_password": "new-demo-password"},
    )
    assert confirmed.status_code == 200, confirmed.text

    old_login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "demo-password"},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "new-demo-password"},
    )
    assert new_login.status_code == 200


def test_password_reset_does_not_disclose_unknown_email(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "missing@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["dev_token"] is None
