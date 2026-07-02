"""Tests for the application-wide API error contract."""

from fastapi.testclient import TestClient

from app.main import app


def test_http_exception_uses_stable_error_detail() -> None:
    client = TestClient(app)

    response = client.get("/api/v1/users/me")

    assert response.status_code == 401
    assert response.json() == {
        "detail": {
            "code": "unauthorized",
            "message": "Missing bearer token",
            "msg": "Missing bearer token",
            "errors": [],
        }
    }


def test_validation_error_lists_invalid_fields() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "not-an-email", "password": "demo-password"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "validation_error"
    assert detail["message"] == detail["msg"]
    assert detail["errors"][0]["location"] == ["body", "email"]
    assert detail["errors"][0]["message"]
    assert detail["errors"][0]["type"]


def test_openapi_documents_shared_error_schema() -> None:
    client = TestClient(app)

    operation = client.get("/openapi.json").json()["paths"]["/api/v1/auth/login"]["post"]

    assert operation["responses"]["401"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert operation["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
