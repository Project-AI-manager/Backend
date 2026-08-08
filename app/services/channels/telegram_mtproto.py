"""Telegram personal-account authentication and MTProto message transport."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon import utils as telegram_utils  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
    ApiIdInvalidError,
    ApiIdPublishedFloodError,
    FloodWaitError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    PhonePasswordFloodError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.types import InputDialogPeer, InputPeerUser  # type: ignore[import-untyped]

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.schemas.channels import (
    TelegramAccountAuthResponse,
    TelegramAccountStartResponse,
    TelegramMTProtoInbound,
    TelegramQRStartResponse,
    TelegramQRStatusResponse,
)
from app.services.channels.base import NormalizedMessage
from app.services.channels.telegram import (
    _get_or_create_conversation,
    _get_or_create_customer,
    process_telegram_inbound_message,
)
from app.services.customer_avatars import remove_customer_avatar, store_customer_avatar


@dataclass
class PendingTelegramAuth:
    client: TelegramClient
    phone: str
    phone_code_hash: str


@dataclass
class PendingTelegramQRAuth:
    client: TelegramClient
    login_task: asyncio.Task[object]


_pending_auth: dict[UUID, PendingTelegramAuth] = {}
_pending_qr_auth: dict[UUID, PendingTelegramQRAuth] = {}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramMTProtoDelivery:
    delivered: bool
    message_id: int | None = None
    read: bool = False


async def cancel_pending_account_connection(channel_id: UUID) -> None:
    """Close and forget an unfinished MTProto authorization flow."""
    pending = _pending_auth.pop(channel_id, None)
    if pending is not None:
        await pending.client.disconnect()
    qr_pending = _pending_qr_auth.pop(channel_id, None)
    if qr_pending is not None:
        if not qr_pending.login_task.done():
            qr_pending.login_task.cancel()
        await qr_pending.client.disconnect()


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
    normalized_phone = _normalize_phone(phone)
    channel = await _get_or_create_mtproto_channel(session, tenant_id)

    pending = _pending_auth.get(channel.id)
    if pending is not None and pending.phone != normalized_phone:
        await cancel_pending_account_connection(channel.id)
        pending = None

    client = (
        pending.client if pending is not None else TelegramClient(StringSession(), api_id, api_hash)
    )
    try:
        await client.connect()
        sent = await client.send_code_request(normalized_phone)
    except FloodWaitError as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Telegram requires waiting {exc.seconds} seconds",
        ) from exc
    except PhoneNumberInvalidError as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Telegram phone number is invalid",
        ) from exc
    except PhoneNumberBannedError as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This phone number is banned by Telegram",
        ) from exc
    except (PhoneNumberFloodError, PhonePasswordFloodError) as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Telegram temporarily blocked login attempts for this phone number",
        ) from exc
    except (ApiIdInvalidError, ApiIdPublishedFloodError) as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Telegram application credentials are invalid or restricted",
        ) from exc
    except SendCodeUnavailableError as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Telegram cannot deliver a login code to this number right now",
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
    delivery_method = _delivery_method(getattr(sent, "type", None))
    next_delivery_method = _delivery_method(getattr(sent, "next_type", None), optional=True)
    timeout_seconds = getattr(sent, "timeout", None)
    masked_phone = _mask_phone(normalized_phone)
    logger.info(
        "Telegram accepted login code request tenant=%s channel=%s phone=%s "
        "delivery=%s next=%s timeout=%s",
        tenant_id,
        channel.id,
        masked_phone,
        delivery_method,
        next_delivery_method,
        timeout_seconds,
    )
    channel.settings = {
        **channel.settings,
        "auth_status": "code_required",
        "phone_masked": masked_phone,
        "transport": "mtproto",
        "code_delivery": delivery_method,
        "code_next_delivery": next_delivery_method,
        "code_resend_after": timeout_seconds,
    }
    await session.commit()
    return TelegramAccountStartResponse(
        channel_id=channel.id,
        status="code_required",
        delivery_method=delivery_method,
        next_delivery_method=next_delivery_method,
        timeout_seconds=timeout_seconds,
        phone_masked=masked_phone,
    )


async def start_qr_account_connection(
    session: AsyncSession,
    tenant_id: UUID,
) -> TelegramQRStartResponse:
    api_id, api_hash = _application_credentials()
    channel = await _get_or_create_mtproto_channel(session, tenant_id)
    await cancel_pending_account_connection(channel.id)

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        qr_login = await client.qr_login()
    except (ApiIdInvalidError, ApiIdPublishedFloodError) as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Telegram application credentials are invalid or restricted",
        ) from exc
    except Exception as exc:
        await client.disconnect()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Telegram QR authorization could not be started",
        ) from exc

    _pending_qr_auth[channel.id] = PendingTelegramQRAuth(
        client=client,
        login_task=asyncio.create_task(qr_login.wait()),
    )
    channel.status = "disabled"
    channel.settings = {
        **channel.settings,
        "auth_status": "qr_waiting",
        "transport": "mtproto",
        "qr_expires_at": qr_login.expires.isoformat(),
    }
    await session.commit()
    return TelegramQRStartResponse(
        channel_id=channel.id,
        qr_url=qr_login.url,
        expires_at=qr_login.expires,
    )


async def get_qr_account_connection_status(
    session: AsyncSession,
    tenant_id: UUID,
    channel_id: UUID,
) -> TelegramQRStatusResponse:
    channel = await _owned_channel(session, tenant_id, channel_id)
    pending = _pending_qr_auth.get(channel.id)
    if pending is None:
        if channel.status == "active":
            return TelegramQRStatusResponse(
                channel_id=channel.id,
                status="active",
                display_name=channel.name,
            )
        raise HTTPException(status.HTTP_409_CONFLICT, "Telegram QR authorization expired")

    await asyncio.sleep(0)
    if not pending.login_task.done():
        return TelegramQRStatusResponse(channel_id=channel.id, status="waiting")

    try:
        pending.login_task.result()
    except SessionPasswordNeededError:
        channel.settings = {**channel.settings, "auth_status": "password_required"}
        await session.commit()
        return TelegramQRStatusResponse(
            channel_id=channel.id,
            status="password_required",
        )
    except TimeoutError:
        _pending_qr_auth.pop(channel.id, None)
        await pending.client.disconnect()
        channel.settings = {**channel.settings, "auth_status": "qr_expired"}
        await session.commit()
        return TelegramQRStatusResponse(channel_id=channel.id, status="expired")
    except Exception as exc:
        _pending_qr_auth.pop(channel.id, None)
        await pending.client.disconnect()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Telegram QR authorization failed",
        ) from exc

    auth = await _complete_auth(session, channel, pending.client)
    return TelegramQRStatusResponse(
        channel_id=auth.channel_id,
        status="active",
        display_name=auth.display_name,
    )


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
    account_id = str(getattr(me, "id", "") or "")
    previous_account_id = str((channel.settings or {}).get("account_id") or "")
    if previous_account_id and account_id != previous_account_id:
        await client.disconnect()
        _pending_auth.pop(channel.id, None)
        _pending_qr_auth.pop(channel.id, None)
        channel.settings = {**channel.settings, "auth_status": "account_mismatch"}
        await session.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Подключите тот же Telegram-аккаунт, к которому относится история диалогов.",
        )
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
        "account_id": account_id,
        "username": str(getattr(me, "username", "") or ""),
    }
    await session.commit()
    _pending_auth.pop(channel.id, None)
    _pending_qr_auth.pop(channel.id, None)
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
        message_id = getattr(sent, "id", None)
        is_read = await _is_outgoing_message_read(client, int(peer_id), message_id)
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
    return TelegramMTProtoDelivery(
        delivered=True,
        message_id=int(message_id) if message_id is not None else None,
        read=is_read,
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
        message_id = getattr(sent, "id", None)
        is_read = await _is_outgoing_message_read(client, int(peer_id), message_id)
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
    return TelegramMTProtoDelivery(
        delivered=True,
        message_id=int(message_id) if message_id is not None else None,
        read=is_read,
    )


async def _is_outgoing_message_read(
    client: TelegramClient,
    peer_id: int,
    message_id: object,
) -> bool:
    """Read Telegram's authoritative outbox watermark after a send.

    A second Telegram session can consume the real-time read update before the
    listener sees it. The dialog watermark still contains the final state.
    """
    if not isinstance(message_id, int):
        return False
    try:
        async for dialog in client.iter_dialogs():
            if int(dialog.id) != peer_id:
                continue
            raw_watermark = getattr(
                getattr(dialog, "dialog", None),
                "read_outbox_max_id",
                0,
            )
            return isinstance(raw_watermark, int) and raw_watermark >= message_id
    except Exception:
        # Delivery already succeeded, so a receipt lookup must not turn it into
        # a transport failure. The long-running listener can still update it.
        return False
    return False


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
    *,
    auto_reply_delay_sec: float = 0.0,
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
    if inbound.avatar_bytes is not None:
        store_customer_avatar(channel.tenant_id, customer.id, inbound.avatar_bytes)
    elif inbound.avatar_checked:
        remove_customer_avatar(channel.tenant_id, customer.id)
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
    if auto_reply_delay_sec > 0:
        await asyncio.sleep(auto_reply_delay_sec)
    await process_telegram_inbound_message(session, message.id)
    return conversation.id


async def sync_mtproto_customer_avatars(
    session: AsyncSession,
    channel: Channel,
    client: TelegramClient,
) -> int:
    """Backfill avatars for customers already known on an MTProto channel.

    Telegram remains the source of truth. A failed download preserves the last
    cached image; a successful lookup with no profile photo removes stale data.
    """
    result = await session.execute(
        select(Customer.id, CustomerIdentity.external_user_id)
        .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
        .where(
            Customer.tenant_id == channel.tenant_id,
            CustomerIdentity.channel_id == channel.id,
        )
    )
    customers_by_external_id = {
        str(external_user_id): customer_id for customer_id, external_user_id in result.all()
    }
    if not customers_by_external_id:
        return 0

    changed = 0
    async for dialog in client.iter_dialogs():
        customer_id = customers_by_external_id.get(str(getattr(dialog, "id", "")))
        if customer_id is None:
            continue
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue
        try:
            if getattr(entity, "photo", None) is None:
                remove_customer_avatar(channel.tenant_id, customer_id)
                changed += 1
                continue
            downloaded = await client.download_profile_photo(entity, file=bytes)
            if isinstance(downloaded, bytes) and store_customer_avatar(
                channel.tenant_id,
                customer_id,
                downloaded,
            ):
                changed += 1
        except Exception:
            # Avatar refresh must never stop message ingestion.
            continue
    return changed


async def mark_mtproto_messages_read(
    session: AsyncSession,
    channel_id: UUID,
    *,
    peer_id: int,
    max_message_id: int,
) -> int:
    """Persist Telegram's read watermark and mark all matching outgoing messages read."""
    return await _mark_mtproto_read_watermarks(
        session,
        channel_id,
        {peer_id: max_message_id},
    )


async def reconcile_mtproto_messages_read(
    session: AsyncSession,
    channel_id: UUID,
    client: TelegramClient,
) -> int:
    """Recover read receipts that were missed while the listener was offline.

    Telegram read updates are transient. In addition, replies are sent through a
    short-lived MTProto connection, so the long-running listener is not guaranteed
    to observe every ``UpdateReadHistoryOutbox`` update. The dialog read watermark
    is durable on Telegram and lets us reconcile those missed updates.
    """
    result = await session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.channel_id == channel_id,
            Message.direction == "outbound",
            Message.status == "sent",
        )
    )
    peer_access_hashes: dict[int, int | None] = {}
    for message in result.scalars().all():
        metadata = message.ai_meta or {}
        raw_peer_id = metadata.get("chat_id")
        try:
            peer_id = int(raw_peer_id)
        except (TypeError, ValueError):
            continue
        raw_access_hash = metadata.get("peer_access_hash")
        peer_access_hashes[peer_id] = raw_access_hash if isinstance(raw_access_hash, int) else None

    if not peer_access_hashes:
        return 0

    input_peers: list[InputDialogPeer] = []
    for peer_id, access_hash in peer_access_hashes.items():
        try:
            peer = await _resolve_peer(client, peer_id, access_hash)
        except (TypeError, ValueError):
            continue
        input_peers.append(InputDialogPeer(peer))

    watermarks: dict[int, int] = {}
    for start in range(0, len(input_peers), 100):
        dialogs = await client(GetPeerDialogsRequest(peers=input_peers[start : start + 100]))
        for dialog in dialogs.dialogs:
            max_id = getattr(dialog, "read_outbox_max_id", None)
            peer = getattr(dialog, "peer", None)
            if not isinstance(max_id, int) or peer is None:
                continue
            peer_id = int(telegram_utils.get_peer_id(peer))
            if peer_id in peer_access_hashes:
                watermarks[peer_id] = max_id

    return await _mark_mtproto_read_watermarks(session, channel_id, watermarks)


async def _mark_mtproto_read_watermarks(
    session: AsyncSession,
    channel_id: UUID,
    new_watermarks: dict[int, int],
) -> int:
    channel = await session.get(Channel, channel_id)
    if channel is None:
        return 0
    settings_json = channel.settings or {}
    raw_watermarks = settings_json.get("read_watermarks")
    watermarks = dict(raw_watermarks) if isinstance(raw_watermarks, dict) else {}
    for peer_id, max_message_id in new_watermarks.items():
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
        try:
            max_message_id = new_watermarks[int(metadata.get("chat_id"))]
        except (KeyError, TypeError, ValueError):
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
        select(Channel)
        .where(Channel.tenant_id == tenant_id, Channel.type == "telegram")
        .order_by(Channel.updated_at.desc())
    )
    channels = list(result.scalars())
    return next(
        (
            channel
            for channel in channels
            if channel.settings.get("transport") == "mtproto"
            or channel.settings.get("auth_status")
            in {"code_required", "password_required", "qr_waiting", "qr_expired"}
            or channel.settings.get("sync_status") == "demo"
        ),
        None,
    )


async def _get_or_create_mtproto_channel(
    session: AsyncSession,
    tenant_id: UUID,
) -> Channel:
    channel = await _tenant_telegram_channel(session, tenant_id)
    if channel is not None:
        return channel
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
    return channel


async def _owned_channel(session: AsyncSession, tenant_id: UUID, channel_id: UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id or channel.type != "telegram":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Telegram channel not found")
    return channel


def _mask_phone(phone: str) -> str:
    value = telegram_utils.parse_phone(phone) or phone.strip()
    return f"***{value[-4:]}" if len(value) >= 4 else "***"


def _normalize_phone(phone: str) -> str:
    parsed = telegram_utils.parse_phone(phone)
    if parsed is None or len(parsed) < 8:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Enter a valid phone number in international format",
        )
    return f"+{parsed}"


def _delivery_method(value: object, *, optional: bool = False) -> str | None:
    if value is None:
        return None if optional else "other"
    name = value.__class__.__name__.lower()
    if "app" in name:
        return "app"
    if "sms" in name or "fragment" in name or "firebase" in name:
        return "sms"
    if "call" in name:
        return "call"
    if "email" in name:
        return "email"
    return "other"
