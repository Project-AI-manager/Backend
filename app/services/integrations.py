"""Diagnostics for external integrations used by the product."""

from __future__ import annotations

from app.core.config import settings
from app.schemas.integrations import IntegrationProbeResponse, IntegrationsHealthResponse
from app.services.rag.embeddings import (
    EmbeddingProviderConfigurationError,
    EmbeddingProviderRequestError,
    get_embedder,
)
from app.services.rag.llm import (
    LLMProviderConfigurationError,
    LLMProviderRequestError,
    OpenAICompatibleProvider,
)
from app.services.rag.vector_store import get_vector_store


def _mask(value: str, *, visible: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "..." + "*" * 6


async def integrations_health(*, probe_llm: bool = False) -> IntegrationsHealthResponse:
    return IntegrationsHealthResponse(
        llm=await probe_llm_provider(probe=probe_llm),
        embeddings=await probe_embedding_provider(probe=False),
        qdrant=await probe_qdrant(),
        email=probe_email(),
        telegram=probe_telegram(),
    )


async def probe_embedding_provider(*, probe: bool = True) -> IntegrationProbeResponse:
    provider_name = settings.EMBEDDING_PROVIDER.strip().lower()
    details = {
        "provider": provider_name,
        "base_url": settings.EMBEDDING_BASE_URL,
        "model": settings.EMBEDDING_MODEL,
        "dimension": settings.EMBEDDING_DIMENSION,
        "api_key": _mask(settings.EMBEDDING_API_KEY),
    }
    try:
        embedder = get_embedder(
            timeout_sec=settings.EMBEDDING_PROBE_TIMEOUT_SEC,
        )
    except EmbeddingProviderConfigurationError as exc:
        return IntegrationProbeResponse(
            name="embeddings",
            status="not_configured",
            message=str(exc),
            details=details,
        )

    if not probe:
        return IntegrationProbeResponse(
            name="embeddings",
            status="ok",
            message=(
                "Deterministic local embeddings are active"
                if embedder.provider_name == "local"
                else "OpenAI-compatible embedding provider is configured"
            ),
            details=details,
        )

    try:
        [vector] = await embedder.embed(["embedding health check"])
    except (EmbeddingProviderConfigurationError, EmbeddingProviderRequestError) as exc:
        return IntegrationProbeResponse(
            name="embeddings",
            status="error",
            message=str(exc),
            details=details,
        )
    return IntegrationProbeResponse(
        name="embeddings",
        status="ok",
        message="Embedding provider returned a valid vector",
        details={**details, "returned_dimension": len(vector)},
    )


async def probe_llm_provider(*, probe: bool = True) -> IntegrationProbeResponse:
    if settings.LLM_PROVIDER == "mock":
        return IntegrationProbeResponse(
            name="llm",
            status="ok",
            message="Mock provider is active",
            details={"provider": "mock"},
        )

    if settings.LLM_PROVIDER.strip().lower() not in {"openai", "openai-compatible", "unirouter"}:
        return IntegrationProbeResponse(
            name="llm",
            status="error",
            message=f"Unsupported provider '{settings.LLM_PROVIDER}'",
            details={"provider": settings.LLM_PROVIDER},
        )

    missing = [
        name
        for name, value in (
            ("OPENAI_COMPATIBLE_BASE_URL", settings.OPENAI_COMPATIBLE_BASE_URL),
            ("OPENAI_COMPATIBLE_API_KEY", settings.OPENAI_COMPATIBLE_API_KEY),
            ("OPENAI_COMPATIBLE_MODEL", settings.OPENAI_COMPATIBLE_MODEL),
        )
        if not value
    ]
    details = {
        "provider": settings.LLM_PROVIDER,
        "base_url": settings.OPENAI_COMPATIBLE_BASE_URL,
        "model": settings.OPENAI_COMPATIBLE_MODEL,
        "api_key": _mask(settings.OPENAI_COMPATIBLE_API_KEY),
    }
    if missing:
        return IntegrationProbeResponse(
            name="llm",
            status="not_configured",
            message=", ".join(missing) + " is required",
            details=details,
        )

    if not probe:
        return IntegrationProbeResponse(
            name="llm",
            status="ok",
            message="OpenAI-compatible provider is configured",
            details=details,
        )

    try:
        provider = OpenAICompatibleProvider(
            base_url=settings.OPENAI_COMPATIBLE_BASE_URL,
            api_key=settings.OPENAI_COMPATIBLE_API_KEY,
            model=settings.OPENAI_COMPATIBLE_MODEL,
            timeout_sec=settings.OPENAI_COMPATIBLE_PROBE_TIMEOUT_SEC,
        )
        answer = await provider.generate(
            "Ответь одним словом: ok",
            [],
            system_prompt="Ты health-check. Верни короткий ответ.",
        )
    except LLMProviderConfigurationError as exc:
        return IntegrationProbeResponse(
            name="llm",
            status="not_configured",
            message=str(exc),
            details=details,
        )
    except LLMProviderRequestError as exc:
        return IntegrationProbeResponse(
            name="llm",
            status="error",
            message=str(exc),
            details=details,
        )

    return IntegrationProbeResponse(
        name="llm",
        status="ok",
        message="OpenAI-compatible provider responded",
        details={**details, "sample": answer[:160]},
    )


async def probe_qdrant() -> IntegrationProbeResponse:
    details = {
        "enabled": settings.QDRANT_ENABLED,
        "url": settings.QDRANT_URL,
        "collection": settings.QDRANT_COLLECTION,
        "mode": (
            "embedded"
            if settings.QDRANT_URL.strip().lower() in {"local", "embedded", ":memory:"}
            else "remote"
        ),
    }
    if not settings.QDRANT_ENABLED:
        return IntegrationProbeResponse(
            name="qdrant",
            status="disabled",
            message="Qdrant is disabled; SQL keyword retrieval is used",
            details=details,
        )

    vector_store = get_vector_store()
    if vector_store is None:
        return IntegrationProbeResponse(
            name="qdrant",
            status="disabled",
            message="Qdrant is disabled; SQL keyword retrieval is used",
            details=details,
        )

    try:
        await vector_store.ensure_collection()
    except Exception as exc:  # noqa: BLE001 - diagnostics must report any client failure.
        return IntegrationProbeResponse(
            name="qdrant",
            status="error",
            message="Qdrant collection is not ready",
            details={**details, "error": str(exc)},
        )

    return IntegrationProbeResponse(
        name="qdrant",
        status="ok",
        message="Qdrant is reachable and collection is ready",
        details=details,
    )


def probe_email() -> IntegrationProbeResponse:
    if not settings.EMAIL_SEND_ENABLED:
        return IntegrationProbeResponse(
            name="email",
            status="disabled",
            message="Email sending is disabled; dev outbox is used",
            details={
                "dev_mode": settings.EMAIL_DEV_MODE,
                "from_email": settings.EMAIL_FROM,
                "smtp_configured": bool(
                    settings.SMTP_HOST
                    and settings.SMTP_USERNAME
                    and settings.SMTP_PASSWORD
                ),
            },
        )
    if not settings.SMTP_HOST:
        return IntegrationProbeResponse(
            name="email",
            status="not_configured",
            message="SMTP_HOST is required when EMAIL_SEND_ENABLED=true",
            details={"from_email": settings.EMAIL_FROM},
        )
    return IntegrationProbeResponse(
        name="email",
        status="ok",
        message="SMTP configuration is present",
        details={
            "host": settings.SMTP_HOST,
            "port": settings.SMTP_PORT,
            "from_email": settings.EMAIL_FROM,
            "use_tls": settings.SMTP_USE_TLS,
            "use_ssl": settings.SMTP_USE_SSL,
        },
    )


def probe_telegram() -> IntegrationProbeResponse:
    return IntegrationProbeResponse(
        name="telegram",
        status="ok" if settings.TELEGRAM_DELIVERY_ENABLED else "disabled",
        message=(
            "Telegram Bot API delivery is enabled"
            if settings.TELEGRAM_DELIVERY_ENABLED
            else "Telegram delivery is disabled; inbound webhook still works locally"
        ),
        details={"delivery_enabled": settings.TELEGRAM_DELIVERY_ENABLED},
    )
