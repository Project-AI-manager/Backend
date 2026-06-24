"""Pydantic schemas for ML message API."""
from typing import Literal

from pydantic import BaseModel, Field


class ChatTurnSchema(BaseModel):
    role: Literal["customer", "manager", "ai", "system"]
    text: str = Field(min_length=1)


class MemorySnippetSchema(BaseModel):
    id: str
    title: str
    text: str
    score: float = Field(ge=0, le=1, default=1.0)
    source: str = "api-override"
    tags: dict[str, str] = Field(default_factory=dict)


class AssistantProfileSchema(BaseModel):
    role_name: str = "AI-менеджер по продажам"
    company_name: str = "компания клиента"
    tone: str = "профессионально, дружелюбно и уверенно"
    language: str = "русский"
    sales_rules: list[str] = Field(default_factory=list)


class MLAnswerRequest(BaseModel):
    tenant_id: str = "demo-tenant"
    message: str = Field(min_length=1)
    history: list[ChatTurnSchema] = Field(default_factory=list)
    profile: AssistantProfileSchema | None = None
    custom_system_prompt: str = ""
    confidence_threshold: int = Field(default=80, ge=0, le=100)
    auto_reply_enabled: bool = False
    memory: list[MemorySnippetSchema] = Field(default_factory=list)


class MLSourceSchema(BaseModel):
    id: str
    title: str
    text: str
    score: float
    source: str
    tags: dict[str, str]


class MLAnswerResponse(BaseModel):
    answer: str
    confidence: float
    decision: Literal["auto_reply", "escalate"]
    provider: str
    sources: list[MLSourceSchema]
    used_context: bool

