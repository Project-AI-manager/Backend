"""Каналы: подключение и вебхуки. Экран: /channels. См. channel-integrations."""

import secrets
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import AdminUser, SessionDep, tenant_id_from_user
from app.core.config import settings
from app.schemas.channels import (
    AvitoOAuthStartResponse,
    ChannelConnectRequest,
    ChannelProbeResponse,
    ChannelResponse,
    ChannelWebhookResponse,
    TelegramAccountAuthResponse,
    TelegramAccountConfirmRequest,
    TelegramAccountPasswordRequest,
    TelegramAccountStartRequest,
    TelegramAccountStartResponse,
    TelegramQRStartResponse,
    TelegramQRStatusResponse,
    VkConnectRequest,
    WhatsAppConnectRequest,
)
from app.services.channels.avito import (
    AVITO_OAUTH_COOKIE,
    complete_avito_oauth,
    consume_avito_oauth_attempt,
    process_avito_webhook,
    start_avito_oauth,
)
from app.services.channels.telegram import (
    connect_channel as connect_channel_service,
)
from app.services.channels.telegram import (
    disconnect_channel as disconnect_channel_service,
)
from app.services.channels.telegram import (
    list_channels as list_channels_service,
)
from app.services.channels.telegram import process_telegram_webhook
from app.services.channels.telegram_mtproto import (
    confirm_account_code,
    confirm_account_password,
    get_qr_account_connection_status,
    start_account_connection,
    start_qr_account_connection,
)
from app.services.channels.vk import connect_vk_channel, process_vk_callback
from app.services.channels.whatsapp import (
    connect_whatsapp_channel,
    probe_whatsapp_channel,
    process_whatsapp_webhook,
    verify_whatsapp_webhook,
)

router = APIRouter()


@router.post("/vk", response_model=ChannelResponse)
async def connect_vk(
    body: VkConnectRequest,
    user: AdminUser,
    session: SessionDep,
) -> ChannelResponse:
    return await connect_vk_channel(session, tenant_id_from_user(user), body)


@router.post("/webhook/vk/{channel_id}", response_class=Response)
async def vk_callback(
    channel_id: uuid.UUID,
    request: Request,
    session: SessionDep,
) -> Response:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid VK callback JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid VK callback payload")
    value, _result = await process_vk_callback(session, channel_id, payload)
    return Response(value, media_type="text/plain")


@router.post("/avito/oauth/start", response_model=AvitoOAuthStartResponse)
async def avito_oauth_start(
    user: AdminUser,
    session: SessionDep,
    response: Response,
) -> AvitoOAuthStartResponse:
    result, browser_binding = await start_avito_oauth(
        session,
        tenant_id_from_user(user),
        uuid.UUID(str(user["sub"])),
        settings.API_PUBLIC_URL,
    )
    response.set_cookie(
        AVITO_OAUTH_COOKIE,
        browser_binding,
        max_age=600,
        httponly=True,
        secure=settings.API_PUBLIC_URL.lower().startswith("https://"),
        samesite="lax",
        path="/api/v1/channels/avito/oauth/callback",
    )
    return result


@router.get("/avito/oauth/callback", response_class=RedirectResponse)
async def avito_oauth_callback(
    request: Request,
    session: SessionDep,
    state_token: Annotated[str, Query(alias="state", min_length=16, max_length=4096)],
    code: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    error: Annotated[str | None, Query(max_length=255)] = None,
) -> RedirectResponse:
    browser_binding = request.cookies.get(AVITO_OAUTH_COOKIE, "")
    if error or not code:
        await consume_avito_oauth_attempt(session, state_token, browser_binding)
        destination = "cancelled" if error in {"access_denied", "cancelled"} else "error"
    else:
        await complete_avito_oauth(
            session,
            code,
            state_token,
            browser_binding,
            settings.API_PUBLIC_URL,
        )
        destination = "connected"
    response = RedirectResponse(
        f"{settings.app_public_href}/channels?avito={destination}", status_code=303
    )
    response.delete_cookie(
        AVITO_OAUTH_COOKIE,
        path="/api/v1/channels/avito/oauth/callback",
    )
    return response


@router.post("/webhook/avito/{webhook_secret}", response_model=ChannelWebhookResponse)
async def avito_webhook(
    webhook_secret: str,
    request: Request,
    session: SessionDep,
) -> ChannelWebhookResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Avito webhook payload")
    return await process_avito_webhook(session, webhook_secret, payload)


@router.post("/whatsapp", response_model=ChannelResponse)
async def connect_whatsapp(
    body: WhatsAppConnectRequest,
    user: AdminUser,
    session: SessionDep,
) -> ChannelResponse:
    return await connect_whatsapp_channel(session, tenant_id_from_user(user), body)


@router.post("/whatsapp/{channel_id}/probe", response_model=ChannelProbeResponse)
async def probe_whatsapp(
    channel_id: uuid.UUID,
    user: AdminUser,
    session: SessionDep,
) -> ChannelProbeResponse:
    return await probe_whatsapp_channel(session, tenant_id_from_user(user), channel_id)


@router.get("/webhook/whatsapp/{phone_number_id}", response_class=Response)
async def verify_whatsapp(
    phone_number_id: str,
    session: SessionDep,
    mode: Annotated[str, Query(alias="hub.mode")],
    verify_token: Annotated[str, Query(alias="hub.verify_token")],
    challenge: Annotated[str, Query(alias="hub.challenge")],
) -> Response:
    value = await verify_whatsapp_webhook(
        session, phone_number_id, mode, verify_token, challenge
    )
    return Response(value, media_type="text/plain")


@router.post("/webhook/whatsapp/{phone_number_id}", response_model=ChannelWebhookResponse)
async def whatsapp_webhook(
    phone_number_id: str,
    request: Request,
    session: SessionDep,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> ChannelWebhookResponse:
    return await process_whatsapp_webhook(
        session, phone_number_id, await request.body(), signature
    )


@router.post("/telegram/account/start", response_model=TelegramAccountStartResponse)
async def start_telegram_account(
    body: TelegramAccountStartRequest,
    user: AdminUser,
    session: SessionDep,
) -> TelegramAccountStartResponse:
    return await start_account_connection(session, tenant_id_from_user(user), body.phone)


@router.post("/telegram/account/qr/start", response_model=TelegramQRStartResponse)
async def start_telegram_qr_account(
    user: AdminUser,
    session: SessionDep,
) -> TelegramQRStartResponse:
    return await start_qr_account_connection(session, tenant_id_from_user(user))


@router.get(
    "/telegram/account/qr/{channel_id}/status",
    response_model=TelegramQRStatusResponse,
)
async def telegram_qr_account_status(
    channel_id: uuid.UUID,
    user: AdminUser,
    session: SessionDep,
) -> TelegramQRStatusResponse:
    return await get_qr_account_connection_status(
        session,
        tenant_id_from_user(user),
        channel_id,
    )


@router.post("/telegram/account/confirm", response_model=TelegramAccountAuthResponse)
async def confirm_telegram_account(
    body: TelegramAccountConfirmRequest,
    user: AdminUser,
    session: SessionDep,
) -> TelegramAccountAuthResponse:
    return await confirm_account_code(
        session, tenant_id_from_user(user), body.channel_id, body.code
    )


@router.post("/telegram/account/password", response_model=TelegramAccountAuthResponse)
async def confirm_telegram_password(
    body: TelegramAccountPasswordRequest,
    user: AdminUser,
    session: SessionDep,
) -> TelegramAccountAuthResponse:
    return await confirm_account_password(
        session, tenant_id_from_user(user), body.channel_id, body.password
    )


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


@router.delete("/{channel_id}", response_model=ChannelResponse)
async def disconnect_channel(
    channel_id: uuid.UUID,
    user: AdminUser,
    session: SessionDep,
) -> ChannelResponse:
    return await disconnect_channel_service(
        session,
        tenant_id_from_user(user),
        channel_id,
    )


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
