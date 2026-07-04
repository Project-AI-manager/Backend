"""Integration diagnostics for ML, Qdrant, email and Telegram."""

from fastapi import APIRouter

from app.schemas.integrations import IntegrationProbeResponse, IntegrationsHealthResponse
from app.services.integrations import integrations_health, probe_llm_provider

router = APIRouter()


@router.get("/health", response_model=IntegrationsHealthResponse)
async def health() -> IntegrationsHealthResponse:
    return await integrations_health(probe_llm=False)


@router.post("/llm/probe", response_model=IntegrationProbeResponse)
async def llm_probe() -> IntegrationProbeResponse:
    return await probe_llm_provider(probe=True)
