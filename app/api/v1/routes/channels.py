"""Каналы: подключение и вебхуки. Экран: /channels. См. channel-integrations."""

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import AdminUser, SessionDep, tenant_id_from_user
from app.core.config import settings
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
async def list_channels(user: AdminUser, session: SessionDep) -> list[ChannelResponse]:
    return await list_channels_service(session, tenant_id_from_user(user))


@router.post("", response_model=ChannelResponse)
async def connect_channel(
    body: ChannelConnectRequest,
    user: AdminUser,
    session: SessionDep,
) -> ChannelResponse:
    return await connect_channel_service(session, tenant_id_from_user(user), body)


@router.post("/webhook/{channel_type}", response_model=ChannelWebhookResponse)
async def webhook(
    channel_type: str,
    request: Request,
    session: SessionDep,
    telegram_secret: Annotated[
        str | None,
        Header(alias="X-Telegram-Bot-Api-Secret-Token"),
    ] = None,
) -> ChannelWebhookResponse:
    if not telegram_secret and not settings.allow_insecure_telegram_webhook:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Webhook not found")
    return await _process_webhook(channel_type, telegram_secret, request, session)


@router.post("/webhook/{channel_type}/{webhook_secret}", response_model=ChannelWebhookResponse)
async def webhook_with_secret(
    channel_type: str,
    webhook_secret: str,
    request: Request,
    session: SessionDep,
    telegram_secret: Annotated[
        str | None,
        Header(alias="X-Telegram-Bot-Api-Secret-Token"),
    ] = None,
) -> ChannelWebhookResponse:
    if telegram_secret and not secrets.compare_digest(telegram_secret, webhook_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook secret")
    return await _process_webhook(channel_type, webhook_secret, request, session)


async def _process_webhook(
    channel_type: str,
    webhook_secret: str | None,
    request: Request,
    session: SessionDep,
) -> ChannelWebhookResponse:
    payload: dict[str, Any] = await request.json()
    if channel_type != "telegram":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unsupported webhook channel")
    return await process_telegram_webhook(session, payload, webhook_secret=webhook_secret)
