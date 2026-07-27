"""Integration diagnostics tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.core.security import create_token
from app.db.session import get_session
from app.main import app
from app.models.tenant import Tenant
from app.models.user import User

TENANT_ID = uuid.UUID("99999999-9999-4999-8999-999999999901")
USER_ID = uuid.UUID("99999999-9999-4999-8999-999999999902")


def create_table(sync_connection: Connection, table: Table) -> None:
    table.create(sync_connection)


@pytest.fixture()
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(create_table, cast(Table, Tenant.__table__))
        await conn.run_sync(create_table, cast(Table, User.__table__))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=TENANT_ID, name="Test", slug="test", status="active"))
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


def test_integrations_health_reports_local_defaults(client: TestClient) -> None:
    response = client.get("/api/v1/integrations/health", headers=auth_headers())

    assert response.status_code == 200
    data = response.json()
    assert data["llm"]["name"] == "llm"
    assert data["embeddings"]["status"] == "ok"
    assert data["qdrant"]["status"] in {"disabled", "ok", "error"}
    assert data["email"]["name"] == "email"
    assert data["telegram"]["name"] == "telegram"


def test_llm_probe_reports_missing_openai_compatible_config(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "unirouter")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "")

    response = client.post("/api/v1/integrations/llm/probe", headers=auth_headers())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_configured"
    assert "OPENAI_COMPATIBLE_BASE_URL" in data["message"]


def test_llm_probe_calls_openai_compatible_provider(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": "ok"}}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "unirouter")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "http://localhost:20128/v1")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "runtime-key")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL", "cx/gpt-5.4-mini")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_PROBE_TIMEOUT_SEC", 3.0)

    response = client.post("/api/v1/integrations/llm/probe", headers=auth_headers())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["details"]["sample"] == "ok"
    assert captured["timeout"] == 3.0
    assert captured["url"] == "http://localhost:20128/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer runtime-key"


def test_qdrant_health_is_disabled_by_default(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)

    response = client.get("/api/v1/integrations/health", headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["qdrant"]["status"] == "disabled"


def test_integration_diagnostics_require_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/integrations/health").status_code == 401
    assert client.post("/api/v1/integrations/llm/probe").status_code == 401
    assert client.post("/api/v1/integrations/embeddings/probe").status_code == 401


def test_embedding_probe_calls_openai_compatible_provider(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://embeddings.test/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer runtime-embedding-key"
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    real_client = httpx.AsyncClient

    def client_factory(*, timeout: float, transport: object = None) -> httpx.AsyncClient:
        return real_client(timeout=timeout, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "openai-compatible")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://embeddings.test/v1")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "runtime-embedding-key")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "small")
    monkeypatch.setattr(settings, "EMBEDDING_DIMENSION", 3)

    response = client.post(
        "/api/v1/integrations/embeddings/probe",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["details"]["returned_dimension"] == 3
    assert data["details"]["api_key"] != "runtime-embedding-key"
