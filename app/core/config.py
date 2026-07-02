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

    S3_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "ai-manager"

    LLM_PROVIDER: str = "mock"
    EMBEDDING_PROVIDER: str = "local"
    YANDEX_API_KEY: str = ""
    GIGACHAT_API_KEY: str = ""
    TELEGRAM_DELIVERY_ENABLED: bool = False
    TELEGRAM_DELIVERY_TIMEOUT_SEC: float = 8.0

    ACCESS_TOKEN_TTL_MIN: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 30

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
