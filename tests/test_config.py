"""Production configuration safeguards."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def production_settings(**overrides: object) -> Settings:
    values: dict[str, Any] = {
        "APP_ENV": "production",
        "SECRET_KEY": "a-random-production-secret-key-with-32-chars",
        "CORS_ORIGINS": "https://app.example.com",
        "EMAIL_DEV_MODE": False,
        "DATABASE_URL": "postgresql+asyncpg://app:app@db:5432/app",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "secret",
    ["", "change-me", "change-me-in-prod", "local-development-secret-key-change-me", "short"],
)
def test_production_rejects_insecure_secret_key(secret: str) -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        production_settings(SECRET_KEY=secret)


def test_production_rejects_local_only_configuration() -> None:
    with pytest.raises(ValidationError, match="CORS_ORIGINS"):
        production_settings(CORS_ORIGINS="*")
    with pytest.raises(ValidationError, match="EMAIL_DEV_MODE"):
        production_settings(EMAIL_DEV_MODE=True)
    with pytest.raises(ValidationError, match="SQLite"):
        production_settings(DATABASE_URL="sqlite+aiosqlite:///./app.db")


def test_local_defaults_remain_available_for_development() -> None:
    local = Settings(_env_file=None)

    assert local.is_local_or_test is True
    assert local.allow_insecure_telegram_webhook is True


@pytest.mark.parametrize(
    ("configured", "href", "display"),
    [
        (
            "https://автопилот.space/",
            "https://xn--80aesmncewf.space",
            "https://автопилот.space",
        ),
        (
            "https://xn--80aesmncewf.space/",
            "https://xn--80aesmncewf.space",
            "https://автопилот.space",
        ),
    ],
)
def test_public_idn_has_separate_protocol_and_display_forms(
    configured: str,
    href: str,
    display: str,
) -> None:
    configured_settings = Settings(_env_file=None, APP_PUBLIC_URL=configured)

    assert configured_settings.app_public_href == href
    assert configured_settings.app_public_display_url == display


@pytest.mark.parametrize(
    "url",
    ["автопилот.space", "javascript:alert(1)", "https://user:pass@example.com"],
)
def test_rejects_unsafe_public_url(url: str) -> None:
    with pytest.raises(ValidationError, match="APP_PUBLIC_URL"):
        Settings(_env_file=None, APP_PUBLIC_URL=url)


def test_production_can_never_enable_secretless_webhook() -> None:
    production = production_settings(TELEGRAM_ALLOW_INSECURE_LOCAL_WEBHOOK=True)

    assert production.allow_insecure_telegram_webhook is False


def test_rejects_enabling_smtp_ssl_and_starttls_together() -> None:
    with pytest.raises(ValidationError, match="SMTP_USE_SSL and SMTP_USE_TLS"):
        Settings(_env_file=None, SMTP_USE_SSL=True, SMTP_USE_TLS=True)


def test_email_delivery_requires_smtp_credentials() -> None:
    with pytest.raises(ValidationError, match="SMTP_PASSWORD"):
        Settings(
            _env_file=None,
            EMAIL_SEND_ENABLED=True,
            SMTP_HOST="smtp.yandex.ru",
            SMTP_USERNAME="autopilot.space",
            SMTP_PASSWORD="",
        )
