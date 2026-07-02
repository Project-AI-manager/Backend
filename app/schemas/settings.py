"""Pydantic schemas for tenant settings endpoints."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

LLM_PROVIDERS = ("mock", "openai-compatible", "unirouter")


class AISettingsResponse(BaseModel):
    auto_reply_enabled: bool
    confidence_threshold: int = Field(ge=0, le=100)
    llm_provider: str
    embedding_model: str
    system_prompt: str
    available_providers: list[str]


class AISettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_reply_enabled: bool | None = None
    confidence_threshold: int | None = Field(default=None, ge=0, le=100)
    llm_provider: str | None = Field(default=None, min_length=1, max_length=32)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=64)
    system_prompt: str | None = Field(default=None, max_length=8000)


class WorkspaceSettingsResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str


class WorkspaceSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class BillingSettingsResponse(BaseModel):
    plan: str
    plan_name: str
    subscription_status: str
    dialogs_used: int
    dialogs_limit: int
    ai_replies_used: int
    channel_limit: int
