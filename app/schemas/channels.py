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


class WhatsAppConnectRequest(BaseModel):
    phone_number_id: str = Field(min_length=1, max_length=64)
    waba_id: str = Field(min_length=1, max_length=64)
    access_token: str = Field(min_length=10, max_length=4096)
    app_secret: str = Field(min_length=8, max_length=512)
    verify_token: str = Field(min_length=8, max_length=512)
    name: str = Field(default="WhatsApp", max_length=255)
    replace_channel_id: UUID | None = None


class ChannelProbeResponse(BaseModel):
    ok: bool
    display_phone_number: str = ""
    verified_name: str = ""


class AvitoOAuthStartResponse(BaseModel):
    authorization_url: str


class VkConnectRequest(BaseModel):
    group_id: int = Field(gt=0)
    access_token: str = Field(min_length=20, max_length=4096)
    callback_confirmation: str = Field(min_length=1, max_length=255)
    callback_secret: str = Field(min_length=8, max_length=255)
    name: str = Field(default="VK", max_length=255)
    replace_channel_id: UUID | None = None


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
    processed_count: int = 0


class TelegramAccountStartRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=32)


class TelegramAccountStartResponse(BaseModel):
    channel_id: UUID
    status: Literal["code_required", "active"]
    delivery_method: Literal["app", "sms", "call", "email", "other"] = "other"
    next_delivery_method: Literal["app", "sms", "call", "email", "other"] | None = None
    timeout_seconds: int | None = None
    phone_masked: str = ""


class TelegramQRStartResponse(BaseModel):
    channel_id: UUID
    status: Literal["waiting"] = "waiting"
    qr_url: str
    expires_at: datetime


class TelegramQRStatusResponse(BaseModel):
    channel_id: UUID
    status: Literal["waiting", "password_required", "active", "expired"]
    display_name: str = ""


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
