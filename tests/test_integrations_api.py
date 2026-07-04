"""Integration diagnostics tests."""

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_integrations_health_reports_local_defaults() -> None:
    response = TestClient(app).get("/api/v1/integrations/health")

    assert response.status_code == 200
    data = response.json()
    assert data["llm"]["name"] == "llm"
    assert data["qdrant"]["status"] in {"disabled", "ok", "error"}
    assert data["email"]["name"] == "email"
    assert data["telegram"]["name"] == "telegram"


def test_llm_probe_reports_missing_openai_compatible_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "unirouter")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "")

    response = TestClient(app).post("/api/v1/integrations/llm/probe")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_configured"
    assert "OPENAI_COMPATIBLE_BASE_URL" in data["message"]


def test_llm_probe_calls_openai_compatible_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
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

    response = TestClient(app).post("/api/v1/integrations/llm/probe")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["details"]["sample"] == "ok"
    assert captured["timeout"] == 3.0
    assert captured["url"] == "http://localhost:20128/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer runtime-key"


def test_qdrant_health_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "QDRANT_ENABLED", False)

    response = TestClient(app).get("/api/v1/integrations/health")

    assert response.status_code == 200
    assert response.json()["qdrant"]["status"] == "disabled"
