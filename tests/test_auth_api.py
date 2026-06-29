"""Auth API tests: register, login, refresh and current user profile."""
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.session import get_session
from app.main import app
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import RefreshToken, User


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Tenant.__table__.create)
        await conn.run_sync(TenantAIConfig.__table__.create)
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(RefreshToken.__table__.create)

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


def register(client: TestClient, email: str = "owner@example.com") -> dict:
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "ООО Север",
            "email": email,
            "password": "demo-password",
            "full_name": "Тимур",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_register_returns_tokens_and_me_profile(client: TestClient) -> None:
    tokens = register(client)

    resp = client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "owner@example.com"
    assert data["full_name"] == "Тимур"
    assert data["role"] == "owner"
    assert data["tenant_id"]


def test_login_returns_new_token_pair(client: TestClient) -> None:
    register(client)

    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "demo-password"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"


def test_refresh_rotates_refresh_token(client: TestClient) -> None:
    tokens = register(client)

    resp = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )

    assert resp.status_code == 200
    rotated = resp.json()
    assert rotated["access_token"]
    assert rotated["refresh_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]

    reused = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": tokens["refresh_token"]},
    )
    assert reused.status_code == 401


def test_register_rejects_duplicate_email(client: TestClient) -> None:
    register(client)

    resp = client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "ООО Север 2",
            "email": "OWNER@example.com",
            "password": "demo-password",
            "full_name": "Другой владелец",
        },
    )

    assert resp.status_code == 409
