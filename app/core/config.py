"""Конфигурация из переменных окружения (pydantic-settings). См. .env.example."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: str = "local"
    SECRET_KEY: str = "change-me"
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
    OPENAI_COMPATIBLE_BASE_URL: str = ""
    OPENAI_COMPATIBLE_API_KEY: str = ""
    OPENAI_COMPATIBLE_MODEL: str = "cx/gpt-5.4-mini"
    OPENAI_COMPATIBLE_TIMEOUT_SEC: float = 30.0
    OPENAI_COMPATIBLE_PROBE_TIMEOUT_SEC: float = 10.0
    YANDEX_API_KEY: str = ""
    GIGACHAT_API_KEY: str = ""
    TELEGRAM_DELIVERY_ENABLED: bool = False
    TELEGRAM_DELIVERY_TIMEOUT_SEC: float = 8.0

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


settings = Settings()
