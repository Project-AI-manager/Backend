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


class TelegramAccountStartRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=32)


class TelegramAccountStartResponse(BaseModel):
    channel_id: UUID
    status: Literal["code_required", "active"]


class TelegramAccountConfirmRequest(BaseModel):
    channel_id: UUID
    code: str = Field(min_length=3, max_length=16)


class TelegramAccountPasswordRequest(BaseModel):
    channel_id: UUID
    password: str = Field(min_length=1, max_length=256)


class TelegramAccountAuthResponse(BaseModel):
    channel_id: UUID
    status: Literal["password_required", "active"]
    display_name: str = ""


class TelegramMTProtoInbound(BaseModel):
    peer_id: int
    peer_access_hash: int | None = None
    sender_id: int
    message_id: int
    text: str
    sender_name: str = ""
    avatar_bytes: bytes | None = None
    avatar_checked: bool = False
