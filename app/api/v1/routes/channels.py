"""Каналы: подключение и вебхуки. Экран: /channels. См. channel-integrations."""

from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.schemas.channels import (
    ChannelConnectRequest,
    ChannelResponse,
    ChannelWebhookResponse,
)
from app.services.channels.telegram import (
    connect_channel as connect_channel_service,
)
from app.services.channels.telegram import (
    list_channels as list_channels_service,
)
from app.services.channels.telegram import process_telegram_webhook

router = APIRouter()


@router.get("", response_model=list[ChannelResponse])
async def list_channels(user: CurrentUser, session: SessionDep) -> list[ChannelResponse]:
    return await list_channels_service(session, tenant_id_from_user(user))


@router.post("", response_model=ChannelResponse)
async def connect_channel(
    body: ChannelConnectRequest,
    user: CurrentUser,
    session: SessionDep,
) -> ChannelResponse:
    return await connect_channel_service(session, tenant_id_from_user(user), body)


@router.post("/webhook/{channel_type}", response_model=ChannelWebhookResponse)
async def webhook(
    channel_type: str,
    request: Request,
    session: SessionDep,
) -> ChannelWebhookResponse:
    return await webhook_with_secret(channel_type, "", request, session)


@router.post("/webhook/{channel_type}/{webhook_secret}", response_model=ChannelWebhookResponse)
async def webhook_with_secret(
    channel_type: str,
    webhook_secret: str,
    request: Request,
    session: SessionDep,
) -> ChannelWebhookResponse:
    payload: dict[str, Any] = await request.json()
    if channel_type != "telegram":
        return ChannelWebhookResponse(ok=False)
    return await process_telegram_webhook(session, payload, webhook_secret=webhook_secret or None)
