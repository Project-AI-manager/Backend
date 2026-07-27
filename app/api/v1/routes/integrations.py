"""Integration diagnostics for ML, Qdrant, email and Telegram."""

from fastapi import APIRouter

from app.api.deps import AdminUser
from app.schemas.integrations import IntegrationProbeResponse, IntegrationsHealthResponse
from app.services.integrations import (
    integrations_health,
    probe_embedding_provider,
    probe_llm_provider,
)

router = APIRouter()


@router.get("/health", response_model=IntegrationsHealthResponse)
async def health(user: AdminUser) -> IntegrationsHealthResponse:
    del user
    return await integrations_health(probe_llm=False)


@router.post("/llm/probe", response_model=IntegrationProbeResponse)
async def llm_probe(user: AdminUser) -> IntegrationProbeResponse:
    del user
    return await probe_llm_provider(probe=True)


@router.post("/embeddings/probe", response_model=IntegrationProbeResponse)
async def embeddings_probe(user: AdminUser) -> IntegrationProbeResponse:
    del user
    return await probe_embedding_provider(probe=True)
