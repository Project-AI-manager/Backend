"""Diagnostics for external integrations used by the product."""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.schemas.integrations import IntegrationProbeResponse, IntegrationsHealthResponse
from app.services.rag.llm import (
    LLMProviderConfigurationError,
    LLMProviderRequestError,
    OpenAICompatibleProvider,
)


def _mask(value: str, *, visible: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "..." + "*" * 6


async def integrations_health(*, probe_llm: bool = False) -> IntegrationsHealthResponse:
    return IntegrationsHealthResponse(
        llm=await probe_llm_provider(probe=probe_llm),
        qdrant=await probe_qdrant(),
        email=probe_email(),
        telegram=probe_telegram(),
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
    }
    if not settings.QDRANT_ENABLED:
        return IntegrationProbeResponse(
            name="qdrant",
            status="disabled",
            message="Qdrant is disabled; SQL keyword retrieval is used",
            details=details,
        )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.QDRANT_URL.rstrip('/')}/collections")
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        return IntegrationProbeResponse(
            name="qdrant",
            status="error",
            message="Qdrant is not reachable",
            details={**details, "error": str(exc)},
        )

    return IntegrationProbeResponse(
        name="qdrant",
        status="ok",
        message="Qdrant is reachable",
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
                "smtp_configured": bool(settings.SMTP_HOST),
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
