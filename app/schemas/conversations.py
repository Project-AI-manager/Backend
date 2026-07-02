"""Pydantic schemas for inbox conversations and messages."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationReplyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ConversationMessageResponse(BaseModel):
    id: UUID
    direction: str
    sender_type: str
    text: str
    status: str
    confidence: float | None
    ai_meta: dict
    created_at: datetime


class ConversationResponse(BaseModel):
    id: UUID
    channel_id: UUID
    customer_id: UUID
    customer_name: str
    status: str
    last_message_at: datetime | None
    last_message_preview: str
    unread_count: int


class ConversationThreadResponse(ConversationResponse):
    messages: list[ConversationMessageResponse]


class ConversationActionResponse(BaseModel):
    conversation: ConversationThreadResponse
    message: ConversationMessageResponse | None = None
    delivered: bool | None = None
