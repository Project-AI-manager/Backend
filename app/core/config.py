"""Конфигурация из переменных окружения (pydantic-settings). См. .env.example."""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_SECRET_KEYS = {
    "",
    "change-me",
    "change-me-in-prod",
    "local-development-secret-key-change-me",
    "secret",
    "dev-secret",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "local"
    SECRET_KEY: str = "local-development-secret-key-change-me"
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = "http://localhost:3000"

    DATABASE_URL: str = "postgresql+asyncpg://app:app@localhost:5432/ai_manager"
    REDIS_URL: str = "redis://localhost:6379/0"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION: str = "ai_manager_knowledge"
    QDRANT_ENABLED: bool = False

    S3_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "ai-manager"

    LLM_PROVIDER: str = "mock"
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1024
    EMBEDDING_TIMEOUT_SEC: float = 30.0
    EMBEDDING_PROBE_TIMEOUT_SEC: float = 10.0
    OPENAI_COMPATIBLE_BASE_URL: str = ""
    OPENAI_COMPATIBLE_API_KEY: str = ""
    OPENAI_COMPATIBLE_MODEL: str = "cx/gpt-5.4-mini"
    OPENAI_COMPATIBLE_TIMEOUT_SEC: float = 30.0
    OPENAI_COMPATIBLE_PROBE_TIMEOUT_SEC: float = 10.0
    YANDEX_API_KEY: str = ""
    GIGACHAT_API_KEY: str = ""
    TELEGRAM_DELIVERY_ENABLED: bool = False
    TELEGRAM_DELIVERY_TIMEOUT_SEC: float = 8.0
    TELEGRAM_ALLOW_INSECURE_LOCAL_WEBHOOK: bool = True

    EMAIL_FROM: str = "Автопилот <no-reply@localhost>"
    EMAIL_DEV_MODE: bool = True
    EMAIL_SEND_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    EMAIL_TOKEN_TTL_MIN: int = 60

    ACCESS_TOKEN_TTL_MIN: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 30

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_local_or_test(self) -> bool:
        return self.APP_ENV.strip().lower() in {"local", "test"}

    @property
    def allow_insecure_telegram_webhook(self) -> bool:
        """The secretless Telegram route can never be enabled outside local/test."""
        return self.is_local_or_test and self.TELEGRAM_ALLOW_INSECURE_LOCAL_WEBHOOK

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        """Fail fast instead of starting a remotely exposed app with unsafe defaults."""
        self.assert_safe_runtime()
        return self

    def assert_safe_runtime(self) -> None:
        """Re-check settings after runtime mutation, mainly at application startup."""
        if self.is_local_or_test:
            return

        secret = self.SECRET_KEY.strip()
        if secret.lower() in _INSECURE_SECRET_KEYS or len(secret) < 32:
            raise ValueError(
                "SECRET_KEY must be a random value of at least 32 characters outside local/test"
            )
        if "*" in self.cors_origins:
            raise ValueError("CORS_ORIGINS cannot contain '*' outside local/test")
        if self.EMAIL_DEV_MODE:
            raise ValueError("EMAIL_DEV_MODE must be false outside local/test")
        if self.EMAIL_SEND_ENABLED and not self.SMTP_HOST.strip():
            raise ValueError("SMTP_HOST is required when EMAIL_SEND_ENABLED=true")
        if self.DATABASE_URL.strip().lower().startswith("sqlite"):
            raise ValueError("SQLite DATABASE_URL is only supported in local/test")


settings = Settings()
