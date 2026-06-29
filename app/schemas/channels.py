"""Pydantic schemas for channel connections and webhooks."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ChannelType = Literal["telegram"]


class ChannelConnectRequest(BaseModel):
    type: ChannelType
    bot_token: str = Field(min_length=10)
    bot_username: str = Field(default="", max_length=255)
    name: str = Field(default="Telegram", max_length=255)


class ChannelResponse(BaseModel):
    id: UUID
    type: str
    name: str
    status: str
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ChannelWebhookResponse(BaseModel):
    ok: bool = True
    duplicate: bool = False
    channel_id: UUID | None = None
    conversation_id: UUID | None = None
    inbound_message_id: UUID | None = None
    outbound_message_id: UUID | None = None
    decision: Literal["auto_reply", "escalate"] | None = None
