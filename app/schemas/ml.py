"""Pydantic schemas for ML message API."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatTurnSchema(BaseModel):
    role: Literal["customer", "manager", "ai", "system"]
    text: str = Field(min_length=1)


class MLAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    history: list[ChatTurnSchema] = Field(default_factory=list)


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
