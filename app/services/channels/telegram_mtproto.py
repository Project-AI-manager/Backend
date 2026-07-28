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
from telethon.tl.types import InputPeerUser  # type: ignore[import-untyped]

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.models.channel import Channel
from app.models.conversation import Conversation, Message
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


@dataclass(frozen=True)
class TelegramMTProtoDelivery:
    delivered: bool
    message_id: int | None = None


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


async def send_mtproto_message(
    channel: Channel,
    peer_id: str,
    text: str,
    *,
    peer_access_hash: int | None = None,
) -> TelegramMTProtoDelivery:
    client: TelegramClient | None = None
    try:
        client = await create_authorized_client(channel)
        peer = await _resolve_peer(client, int(peer_id), peer_access_hash)
        sent = await client.send_message(peer, text)
    except FloodWaitError:
        return TelegramMTProtoDelivery(delivered=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            (
                "Не удалось найти получателя в Telegram. "
                "Получите от него новое сообщение и повторите отправку."
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Не удалось отправить сообщение в Telegram. Попробуйте ещё раз.",
        ) from exc
    finally:
        if client is not None:
            await client.disconnect()
    message_id = getattr(sent, "id", None)
    return TelegramMTProtoDelivery(
        delivered=True,
        message_id=int(message_id) if message_id is not None else None,
    )


async def send_mtproto_file(
    channel: Channel,
    peer_id: str,
    file_path: str,
    caption: str,
    *,
    peer_access_hash: int | None = None,
    force_document: bool = True,
) -> TelegramMTProtoDelivery:
    client: TelegramClient | None = None
    try:
        client = await create_authorized_client(channel)
        peer = await _resolve_peer(client, int(peer_id), peer_access_hash)
        sent = await client.send_file(
            peer,
            file_path,
            caption=caption or None,
            force_document=force_document,
        )
    except FloodWaitError:
        return TelegramMTProtoDelivery(delivered=False)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Не удалось отправить вложение в Telegram. Попробуйте ещё раз.",
        ) from exc
    finally:
        if client is not None:
            await client.disconnect()
    message_id = getattr(sent, "id", None)
    return TelegramMTProtoDelivery(
        delivered=True,
        message_id=int(message_id) if message_id is not None else None,
    )


async def _resolve_peer(
    client: TelegramClient,
    peer_id: int,
    peer_access_hash: int | None,
) -> object:
    if peer_access_hash is not None:
        return InputPeerUser(user_id=peer_id, access_hash=peer_access_hash)

    try:
        return await client.get_input_entity(peer_id)
    except ValueError:
        # StringSession intentionally does not persist Telegram's entity cache.
        # Refresh the dialogs so replies to conversations created by an older
        # application version can still resolve their recipient.
        async for dialog in client.iter_dialogs():
            if int(dialog.id) == peer_id:
                return dialog.input_entity
        raise


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
        ai_meta={
            "source": "telegram",
            "chat_id": str(inbound.peer_id),
            "transport": "mtproto",
            **(
                {"peer_access_hash": inbound.peer_access_hash}
                if inbound.peer_access_hash is not None
                else {}
            ),
        },
    )
    session.add(message)
    await session.commit()
    await process_telegram_inbound_message(session, message.id)
    return conversation.id


async def mark_mtproto_messages_read(
    session: AsyncSession,
    channel_id: UUID,
    *,
    peer_id: int,
    max_message_id: int,
) -> int:
    """Persist Telegram's read watermark and mark all matching outgoing messages read."""
    channel = await session.get(Channel, channel_id)
    if channel is None:
        return 0
    settings_json = channel.settings or {}
    raw_watermarks = settings_json.get("read_watermarks")
    watermarks = dict(raw_watermarks) if isinstance(raw_watermarks, dict) else {}
    watermark_key = str(peer_id)
    previous = watermarks.get(watermark_key)
    previous_id = previous if isinstance(previous, int) else 0
    watermarks[watermark_key] = max(previous_id, max_message_id)
    channel.settings = {**settings_json, "read_watermarks": watermarks}

    result = await session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.channel_id == channel_id,
            Message.direction == "outbound",
            Message.status == "sent",
        )
    )
    changed = 0
    for message in result.scalars().all():
        metadata = message.ai_meta or {}
        telegram_message_id = metadata.get("telegram_message_id")
        if str(metadata.get("chat_id") or "") != str(peer_id):
            continue
        if not isinstance(telegram_message_id, int) or telegram_message_id > max_message_id:
            continue
        message.status = "read"
        changed += 1
    await session.commit()
    return changed


async def apply_mtproto_read_watermark(
    session: AsyncSession,
    channel_id: UUID,
    message: Message,
) -> bool:
    """Close the send/read race when Telegram reports a read before send is committed."""
    channel = await session.get(Channel, channel_id, populate_existing=True)
    metadata = message.ai_meta or {}
    if channel is None:
        return False
    raw_watermarks = (channel.settings or {}).get("read_watermarks")
    if not isinstance(raw_watermarks, dict):
        return False
    chat_id = str(metadata.get("chat_id") or "")
    telegram_message_id = metadata.get("telegram_message_id")
    watermark = raw_watermarks.get(chat_id)
    if (
        message.status != "sent"
        or not isinstance(telegram_message_id, int)
        or not isinstance(watermark, int)
        or telegram_message_id > watermark
    ):
        return False
    message.status = "read"
    await session.commit()
    return True


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
