"""Integration diagnostics schemas."""

from typing import Any, Literal

from pydantic import BaseModel

IntegrationStatus = Literal["ok", "disabled", "not_configured", "error"]


class IntegrationProbeResponse(BaseModel):
    name: str
    status: IntegrationStatus
    message: str
    details: dict[str, Any] = {}


class IntegrationsHealthResponse(BaseModel):
    llm: IntegrationProbeResponse
    qdrant: IntegrationProbeResponse
    email: IntegrationProbeResponse
    telegram: IntegrationProbeResponse
