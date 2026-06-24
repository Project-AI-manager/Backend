"""API smoke tests for /api/v1/ml/answer."""
from fastapi.testclient import TestClient

from app.core.security import create_token
from app.main import app


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token('test-user')}"}


def test_ml_answer_endpoint_returns_mock_response() -> None:
    client = TestClient(app)

    resp = client.post(
        "/api/v1/ml/answer",
        headers=auth_headers(),
        json={
            "tenant_id": "demo",
            "message": "iPhone 15 есть в наличии?",
            "auto_reply_enabled": True,
            "confidence_threshold": 50,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "mock"
    assert data["decision"] == "auto_reply"
    assert data["sources"]
    assert "iPhone" in data["answer"]


def test_knowledge_ask_reuses_ml_flow() -> None:
    client = TestClient(app)

    resp = client.post(
        "/api/v1/knowledge/ask",
        headers=auth_headers(),
        json={
            "tenant_id": "demo",
            "message": "Как работает доставка по Казани?",
            "auto_reply_enabled": False,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "mock"
    assert data["decision"] == "escalate"
    assert data["used_context"] is True
