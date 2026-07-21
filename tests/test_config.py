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


def test_production_can_never_enable_secretless_webhook() -> None:
    production = production_settings(TELEGRAM_ALLOW_INSECURE_LOCAL_WEBHOOK=True)

    assert production.allow_insecure_telegram_webhook is False
