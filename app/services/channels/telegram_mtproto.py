"""Telegram personal-account authentication and MTProto message transport."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
    FloodWaitError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.models.channel import Channel
from app.models.conversation import Message
from app.schemas.channels import (
    TelegramAccountAuthResponse,
    TelegramAccountStartResponse,
    TelegramMTProtoInbound,
)
from app.services.channels.base import NormalizedMessage
from app.services.channels.telegram import (
    _get_or_create_conversation,
    _get_or_create_customer,
    process_telegram_inbound_message,
)


@dataclass
class PendingTelegramAuth:
    client: TelegramClient
    phone: str
    phone_code_hash: str


_pending_auth: dict[UUID, PendingTelegramAuth] = {}


def _application_credentials() -> tuple[int, str]:
    if settings.TELEGRAM_API_ID <= 0 or not settings.TELEGRAM_API_HASH.strip():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Telegram MTProto application credentials are not configured",
        )
    return settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH.strip()


async def start_account_connection(
    session: AsyncSession,
    tenant_id: UUID,
    phone: str,
) -> TelegramAccountStartResponse:
    api_id, api_hash = _application_credentials()
    normalized_phone = phone.strip()
    channel = await _tenant_telegram_channel(session, tenant_id)
    if channel is None:
        channel = Channel(
            tenant_id=tenant_id,
            type="telegram",
            name="Telegram account",
            status="disabled",
            credentials_encrypted="",
            settings={},
        )
        session.add(channel)
        await session.flush()

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(normalized_phone)
    except FloodWaitError as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Telegram requires waiting {exc.seconds} seconds",
        ) from exc
    except Exception as exc:
        await client.disconnect()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Telegram code request failed") from exc

    _pending_auth[channel.id] = PendingTelegramAuth(
        client=client,
        phone=normalized_phone,
        phone_code_hash=sent.phone_code_hash,
    )
    channel.status = "disabled"
    channel.settings = {"auth_status": "code_required", "phone_masked": _mask_phone(phone)}
    await session.commit()
    return TelegramAccountStartResponse(channel_id=channel.id, status="code_required")


async def confirm_account_code(
    session: AsyncSession,
    tenant_id: UUID,
    channel_id: UUID,
    code: str,
) -> TelegramAccountAuthResponse:
    channel = await _owned_channel(session, tenant_id, channel_id)
    pending = _pending_auth.get(channel.id)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Telegram authorization must be restarted")
    try:
        await pending.client.sign_in(
            phone=pending.phone,
            code=code.strip(),
            phone_code_hash=pending.phone_code_hash,
        )
    except SessionPasswordNeededError:
        channel.settings = {**channel.settings, "auth_status": "password_required"}
        await session.commit()
        return TelegramAccountAuthResponse(channel_id=channel.id, status="password_required")
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Telegram code") from exc
    return await _complete_auth(session, channel, pending.client)


async def confirm_account_password(
    session: AsyncSession,
    tenant_id: UUID,
    channel_id: UUID,
    password: str,
) -> TelegramAccountAuthResponse:
    channel = await _owned_channel(session, tenant_id, channel_id)
    pending = _pending_auth.get(channel.id)
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Telegram authorization must be restarted")
    try:
        await pending.client.sign_in(password=password)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Telegram 2FA password") from exc
    return await _complete_auth(session, channel, pending.client)


async def _complete_auth(
    session: AsyncSession,
    channel: Channel,
    client: TelegramClient,
) -> TelegramAccountAuthResponse:
    me = await client.get_me()
    display_name = " ".join(
        part
        for part in (
            str(getattr(me, "first_name", "") or "").strip(),
            str(getattr(me, "last_name", "") or "").strip(),
        )
        if part
    )
    channel.credentials_encrypted = encrypt_secret(client.session.save())
    channel.status = "active"
    channel.name = display_name or str(getattr(me, "username", "") or "Telegram account")
    channel.settings = {
        **channel.settings,
        "auth_status": "active",
        "transport": "mtproto",
        "account_id": str(getattr(me, "id", "")),
        "username": str(getattr(me, "username", "") or ""),
    }
    await session.commit()
    _pending_auth.pop(channel.id, None)
    await client.disconnect()
    return TelegramAccountAuthResponse(
        channel_id=channel.id,
        status="active",
        display_name=channel.name,
    )


async def create_authorized_client(channel: Channel) -> TelegramClient:
    api_id, api_hash = _application_credentials()
    session_string = decrypt_secret(channel.credentials_encrypted)
    if not session_string:
        raise RuntimeError("Telegram MTProto session is missing")
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram MTProto session is no longer authorized")
    return client


async def send_mtproto_message(channel: Channel, peer_id: str, text: str) -> bool:
    client = await create_authorized_client(channel)
    try:
        await client.send_message(int(peer_id), text)
    except FloodWaitError:
        return False
    finally:
        await client.disconnect()
    return True


async def ingest_mtproto_message(
    session: AsyncSession,
    channel: Channel,
    inbound: TelegramMTProtoInbound,
) -> UUID:
    normalized = NormalizedMessage(
        channel="telegram",
        external_conversation_id=str(inbound.peer_id),
        external_message_id=f"mtproto:{inbound.peer_id}:{inbound.message_id}",
        customer_ref=str(inbound.sender_id),
        customer_name=inbound.sender_name or str(inbound.sender_id),
        text=inbound.text.strip(),
        attachments={},
    )
    customer = await _get_or_create_customer(session, channel, normalized)
    conversation = await _get_or_create_conversation(session, channel, customer, normalized)
    existing = await session.execute(
        select(Message).where(
            Message.conversation_id == conversation.id,
            Message.external_message_id == normalized.external_message_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return conversation.id
    message = Message(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        sender_type="customer",
        sender_user_id=None,
        text=normalized.text,
        attachments={},
        external_message_id=normalized.external_message_id,
        status="received",
        confidence=None,
        ai_meta={"source": "telegram", "chat_id": str(inbound.peer_id), "transport": "mtproto"},
    )
    session.add(message)
    await session.commit()
    await process_telegram_inbound_message(session, message.id)
    return conversation.id


async def _tenant_telegram_channel(session: AsyncSession, tenant_id: UUID) -> Channel | None:
    result = await session.execute(
        select(Channel).where(Channel.tenant_id == tenant_id, Channel.type == "telegram")
    )
    channels = list(result.scalars())
    return next(
        (
            channel
            for channel in channels
            if channel.settings.get("transport") == "mtproto"
            or channel.settings.get("sync_status") == "demo"
        ),
        None,
    )


async def _owned_channel(session: AsyncSession, tenant_id: UUID, channel_id: UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id or channel.type != "telegram":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Telegram channel not found")
    return channel


def _mask_phone(phone: str) -> str:
    value = phone.strip()
    return f"***{value[-4:]}" if len(value) >= 4 else "***"
