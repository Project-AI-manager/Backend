"""Email module API tests."""

import asyncio
import smtplib
import uuid
from collections.abc import AsyncGenerator, Generator
from email import message_from_bytes
from email.policy import default
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.schema import Table

from app.core.config import settings
from app.db.session import get_session
from app.main import app
from app.models.email import EmailOutbox, EmailToken
from app.models.ops import BillingAccount
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import RefreshToken, User
from app.services.email_assets import (
    AUTOPILOT_LOGO_CID,
    AUTOPILOT_LOGO_FILENAME,
    AUTOPILOT_LOGO_PNG,
)
from app.services.email_templates import (
    escalation_email,
    password_reset_email,
    verification_email,
)


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
            BillingAccount.__table__,
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

    original_dev_mode = settings.EMAIL_DEV_MODE
    original_send_enabled = settings.EMAIL_SEND_ENABLED
    settings.EMAIL_DEV_MODE = True
    settings.EMAIL_SEND_ENABLED = False
    app.dependency_overrides[get_session] = override_get_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)
        settings.EMAIL_DEV_MODE = original_dev_mode
        settings.EMAIL_SEND_ENABLED = original_send_enabled


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


async def set_role(
    session_factory: async_sessionmaker[AsyncSession],
    role: str,
) -> None:
    async with session_factory() as session:
        user = (await session.execute(select(User))).scalar_one()
        user.role = role
        await session.commit()


def test_email_status_is_public(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")

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
    assert len(dev_token) == 6
    assert dev_token.isdecimal()

    confirmed = client.post("/api/v1/email/verification/confirm", json={"token": dev_token})
    assert confirmed.status_code == 200, confirmed.text

    me = client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email_verified"] is True

    outbox = client.get("/api/v1/email/outbox", headers=headers)
    assert outbox.status_code == 200, outbox.text
    assert outbox.json()[0]["purpose"] == "verify_email"
    assert outbox.json()[0]["status"] == "dev"


def test_email_verification_sends_six_digit_code_over_configured_smtp(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = register(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    delivered: list[EmailOutbox] = []

    monkeypatch.setattr(settings, "EMAIL_SEND_ENABLED", True)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(
        "app.services.email._send_smtp",
        lambda outbox: delivered.append(outbox),
    )

    requested = client.post("/api/v1/email/verification/request", headers=headers)

    assert requested.status_code == 200, requested.text
    payload = requested.json()
    assert payload["sent"] is True
    assert len(delivered) == 1
    code = payload["dev_token"]
    assert code is not None
    assert code.isdecimal() and len(code) == 6
    assert f"\n{code}\n" in delivered[0].body_text
    assert "Ваш код подтверждения для входа в Автопилот" in delivered[0].body_text
    assert "Код действует 60 минут" in delivered[0].body_text
    assert "<!doctype html>" in delivered[0].metadata_json["body_html"]
    assert code in delivered[0].metadata_json["body_html"]
    assert delivered[0].status == "sent"


def test_smtp_ssl_delivery_uses_authenticated_yandex_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int, context) -> None:
            assert context is not None
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            calls.append(("login", username, password))

        def send_message(self, message) -> None:
            html_body = message.get_body(preferencelist=("html",))
            logo_part = next(
                part
                for part in message.walk()
                if part.get_content_type() == "image/png"
            )
            calls.append((
                "send",
                message["From"],
                message["To"],
                message["Subject"],
                message.get_content_type(),
                html_body.get_content_charset(),
                logo_part["Content-ID"],
                logo_part["Content-Location"],
                logo_part["X-Attachment-Id"],
                logo_part.get_content_disposition(),
                logo_part.get_param("name"),
                logo_part.get_content_type(),
                logo_part.get_payload(decode=True),
            ))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setattr(settings, "SMTP_PORT", 465)
    monkeypatch.setattr(settings, "SMTP_USE_SSL", True)
    monkeypatch.setattr(settings, "SMTP_USE_TLS", False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "autopilot.space@yandex.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(settings, "EMAIL_FROM", "Автопилот <autopilot.space@yandex.com>")

    from app.services.email import _send_smtp

    _send_smtp(
        EmailOutbox(
            to_email="recipient@example.com",
            subject="Ваш код подтверждения",
            body_text="Ваш код: 123456",
            purpose="verify_email",
            metadata_json={
                "body_html": (
                    f'<p><img src="cid:{AUTOPILOT_LOGO_CID}" alt="">'
                    "Ваш код: <strong>123456</strong></p>"
                )
            },
        )
    )

    assert calls == [
        ("connect", "smtp.yandex.ru", 465, 10),
        ("login", "autopilot.space@yandex.com", "app-password"),
        (
            "send",
            "Автопилот <autopilot.space@yandex.com>",
            "recipient@example.com",
            "Ваш код подтверждения",
            "multipart/alternative",
            "utf-8",
            f"<{AUTOPILOT_LOGO_CID}>",
            AUTOPILOT_LOGO_FILENAME,
            AUTOPILOT_LOGO_CID,
            "inline",
            AUTOPILOT_LOGO_FILENAME,
            "image/png",
            AUTOPILOT_LOGO_PNG,
        ),
    ]


def test_verification_template_references_embedded_brand_logo() -> None:
    _text, html = verification_email(name="Тимур", code="123456", ttl_minutes=60)

    assert f'src="cid:{AUTOPILOT_LOGO_CID}"' in html
    assert AUTOPILOT_LOGO_PNG.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(AUTOPILOT_LOGO_PNG) > 4000


def test_embedded_logo_is_valid_112_pixel_png() -> None:
    # Validate the PNG dimensions without introducing Pillow as a runtime dependency.
    assert AUTOPILOT_LOGO_PNG[12:16] == b"IHDR"
    assert int.from_bytes(AUTOPILOT_LOGO_PNG[16:20], "big") == 112
    assert int.from_bytes(AUTOPILOT_LOGO_PNG[20:24], "big") == 112


def test_smtp_message_serializes_russian_headers_and_body_as_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_messages: list[bytes] = []

    class FakeSMTP:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def login(self, *_args: object) -> None:
            return None

        def send_message(self, message) -> None:
            raw_messages.append(message.as_bytes())

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.yandex.ru")
    monkeypatch.setattr(settings, "SMTP_PORT", 465)
    monkeypatch.setattr(settings, "SMTP_USE_SSL", True)
    monkeypatch.setattr(settings, "SMTP_USE_TLS", False)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "sender@example.com")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(settings, "EMAIL_FROM", "Автопилот <sender@example.com>")

    from app.services.email import _send_smtp

    _send_smtp(
        EmailOutbox(
            to_email="recipient@example.com",
            subject="Автопилот: требуется ответ менеджера",
            body_text="Клиент Алексей ждёт ответа менеджера.",
            purpose="escalation_alert",
            metadata_json={},
        )
    )

    parsed = message_from_bytes(raw_messages[0], policy=default)
    assert parsed["Subject"] == "Автопилот: требуется ответ менеджера"
    assert parsed["From"] == "Автопилот <sender@example.com>"
    assert parsed.get_body(preferencelist=("plain",)).get_content() == (
        "Клиент Алексей ждёт ответа менеджера.\r\n"
    )


def test_password_reset_template_has_utf8_code_and_embedded_brand_logo() -> None:
    text, html = password_reset_email(code="654321", ttl_minutes=60)

    assert "Ваш код для сброса пароля в Автопилоте" in text
    assert "654321" in text
    assert "Код действует 60 минут" in text
    assert "Сброс пароля" in html
    assert "654321" in html
    assert f'src="cid:{AUTOPILOT_LOGO_CID}"' in html


def test_email_verification_rejects_unknown_code_in_russian(client: TestClient) -> None:
    response = client.post(
        "/api/v1/email/verification/confirm",
        json={"token": "000000"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == ("Неверный или просроченный код подтверждения")


def test_escalation_template_contains_utf8_customer_message_and_chat_link() -> None:
    conversation_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    url = f"https://app.autopilot.space/inbox?conversation={conversation_id}"

    text, html = escalation_email(
        customer_name="Иван Петров",
        message_preview="Подскажите, когда приедет заказ?",
        conversation_url=url,
    )

    assert "Иван Петров" in text
    assert "Подскажите, когда приедет заказ?" in text
    assert url in text
    assert "Открыть диалог" in html
    assert url in html


def test_escalation_template_displays_unicode_idn_but_uses_ascii_href() -> None:
    unicode_url = "https://автопилот.space/inbox?conversation=example"
    ascii_url = "https://xn--80aesmncewf.space/inbox?conversation=example"

    text, html = escalation_email(
        customer_name="Иван Петров",
        message_preview="Нужна помощь",
        conversation_url=unicode_url,
    )

    assert f"Открыть диалог: {unicode_url}" in text
    assert f'href="{ascii_url}"' in html
    assert "Открыть диалог на автопилот.space" in html
    assert "xn--80aesmncewf.space" not in text


def test_escalation_template_omits_unreachable_localhost_link() -> None:
    text, html = escalation_email(
        customer_name="Иван Петров",
        message_preview="Нужна помощь",
        conversation_url="http://localhost:3000/inbox?conversation=example",
    )

    assert "localhost" not in text
    assert "localhost" not in html
    assert "Открыть диалог" not in html


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


def test_manager_cannot_read_email_outbox(
    client: TestClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tokens = register(client)
    asyncio.run(set_role(session_factory, "manager"))

    response = client.get(
        "/api/v1/email/outbox",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert response.status_code == 403
