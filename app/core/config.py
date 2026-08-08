"""Конфигурация из переменных окружения (pydantic-settings). См. .env.example."""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.public_urls import public_url_display, public_url_href, validate_public_url

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
    QDRANT_LOCAL_PATH: str = ".qdrant-data"
    QDRANT_COLLECTION: str = "ai_manager_knowledge"
    QDRANT_ENABLED: bool = False

    S3_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET: str = "ai-manager"
    CONVERSATION_UPLOAD_DIR: str = ".conversation-uploads"
    CONVERSATION_ATTACHMENT_MAX_BYTES: int = 10 * 1024 * 1024
    CUSTOMER_AVATAR_DIR: str = ".customer-avatars"
    CUSTOMER_AVATAR_MAX_BYTES: int = 5 * 1024 * 1024

    LLM_PROVIDER: str = "mock"
    EMBEDDING_PROVIDER: str = "local"
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    EMBEDDING_DIMENSION: int = 384
    EMBEDDING_CACHE_DIR: str = ".model-cache"
    EMBEDDING_TIMEOUT_SEC: float = 30.0
    EMBEDDING_PROBE_TIMEOUT_SEC: float = 10.0
    OPENAI_COMPATIBLE_BASE_URL: str = ""
    OPENAI_COMPATIBLE_API_KEY: str = ""
    OPENAI_COMPATIBLE_MODEL: str = "cx/gpt-5.4-mini"
    OPENAI_COMPATIBLE_TIMEOUT_SEC: float = 30.0
    OPENAI_COMPATIBLE_PROBE_TIMEOUT_SEC: float = 10.0
    ML_RATE_LIMIT_PER_MINUTE: int = 20
    TELEGRAM_CHAT_RATE_LIMIT_PER_MINUTE: int = 30
    TENANT_LLM_CALLS_PER_HOUR: int = 300
    TENANT_LLM_TOKENS_PER_DAY: int = 1_000_000
    TENANT_LLM_COST_KOPECKS_PER_DAY: int = 100_000
    YANDEX_API_KEY: str = ""
    GIGACHAT_API_KEY: str = ""
    TELEGRAM_DELIVERY_ENABLED: bool = False
    TELEGRAM_DELIVERY_TIMEOUT_SEC: float = 8.0
    TELEGRAM_AUTO_REPLY_DELAY_SEC: float = 5.0
    TELEGRAM_ALLOW_INSECURE_LOCAL_WEBHOOK: bool = True
    TELEGRAM_LISTENER_IN_PROCESS: bool = False
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = ""
    WHATSAPP_GRAPH_BASE_URL: str = "https://graph.facebook.com"
    WHATSAPP_DELIVERY_TIMEOUT_SEC: float = 8.0
    AVITO_API_BASE_URL: str = "https://api.avito.ru"
    AVITO_OAUTH_AUTHORIZE_URL: str = "https://avito.ru/oauth"
    AVITO_CLIENT_ID: str = ""
    AVITO_CLIENT_SECRET: str = ""
    AVITO_DELIVERY_TIMEOUT_SEC: float = 8.0
    VK_API_BASE_URL: str = "https://api.vk.ru/method"
    VK_API_VERSION: str = "5.131"
    VK_DELIVERY_TIMEOUT_SEC: float = 8.0

    EMAIL_FROM: str = "Автопилот <no-reply@localhost>"
    EMAIL_REPLY_TO: str = ""
    EMAIL_DEV_MODE: bool = True
    EMAIL_SEND_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    EMAIL_TOKEN_TTL_MIN: int = 60
    CONVERSATION_SSE_POLL_INTERVAL_SEC: float = 1.0
    APP_PUBLIC_URL: str = "http://localhost:3000"
    API_PUBLIC_URL: str = "http://localhost:8000"

    ACCESS_TOKEN_TTL_MIN: int = 30
    REFRESH_TOKEN_TTL_DAYS: int = 30

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        production_frontends = [
            "https://frontend-nine-mu-rjbjzqe6rq.vercel.app",
            "https://frontend-timurzakirov239s-projects.vercel.app",
            "https://frontend-git-main-timurzakirov239s-projects.vercel.app",
            "https://автопилот.space",
            "https://xn--80aesmncewf.space",
            "https://www.xn--80aesmncewf.space",
        ]
        for origin in production_frontends:
            if origin not in origins:
                origins.append(origin)
        return origins

    @property
    def is_local_or_test(self) -> bool:
        return self.APP_ENV.strip().lower() in {"local", "test"}

    @property
    def allow_insecure_telegram_webhook(self) -> bool:
        """The secretless Telegram route can never be enabled outside local/test."""
        return self.is_local_or_test and self.TELEGRAM_ALLOW_INSECURE_LOCAL_WEBHOOK

    @property
    def app_public_href(self) -> str:
        """Protocol-safe public URL (IDN hostname encoded as ASCII/Punycode)."""
        return public_url_href(self.APP_PUBLIC_URL).rstrip("/")

    @property
    def app_public_display_url(self) -> str:
        """Human-readable public URL (IDN hostname decoded to Unicode)."""
        return public_url_display(self.APP_PUBLIC_URL).rstrip("/")

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        """Fail fast instead of starting a remotely exposed app with unsafe defaults."""
        validate_public_url(self.APP_PUBLIC_URL)
        if self.SMTP_USE_SSL and self.SMTP_USE_TLS:
            raise ValueError("SMTP_USE_SSL and SMTP_USE_TLS cannot both be enabled")
        if self.EMAIL_SEND_ENABLED:
            self._assert_smtp_delivery_configured()
        self.assert_safe_runtime()
        return self

    def _assert_smtp_delivery_configured(self) -> None:
        missing_smtp = [
            name
            for name, value in (
                ("SMTP_HOST", self.SMTP_HOST),
                ("SMTP_USERNAME", self.SMTP_USERNAME),
                ("SMTP_PASSWORD", self.SMTP_PASSWORD),
            )
            if not value.strip()
        ]
        if missing_smtp:
            raise ValueError("Email sending requires " + ", ".join(missing_smtp))

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
        if self.DATABASE_URL.strip().lower().startswith("sqlite"):
            raise ValueError("SQLite DATABASE_URL is only supported in local/test")
        if self.TELEGRAM_LISTENER_IN_PROCESS:
            raise ValueError("TELEGRAM_LISTENER_IN_PROCESS is only supported in local/test")


settings = Settings()
