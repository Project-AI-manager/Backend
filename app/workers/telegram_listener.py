"""Long-running Telegram MTProto listener for all active personal-account channels.

Run separately from the API: ``python -m app.workers.telegram_listener``.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from uuid import UUID

from sqlalchemy import select
from telethon import events  # type: ignore[import-untyped]

from app.core.config import settings
from app.core.logging import log
from app.db.session import SessionLocal
from app.models.channel import Channel
from app.schemas.channels import TelegramMTProtoInbound
from app.services.channels.telegram_mtproto import (
    create_authorized_client,
    ingest_mtproto_message,
    mark_mtproto_messages_read,
    reconcile_mtproto_messages_read,
    sync_mtproto_customer_avatars,
)
from app.services.rag.vector_store import close_vector_stores


async def _run_channel(channel: Channel) -> None:
    client = await create_authorized_client(channel)

    async with SessionLocal() as session:
        live_channel = await session.get(Channel, channel.id)
        if live_channel is not None and live_channel.status == "active":
            try:
                refreshed = await sync_mtproto_customer_avatars(session, live_channel, client)
                if refreshed:
                    log.info(
                        "telegram_customer_avatars_refreshed",
                        channel_id=str(channel.id),
                        customers=refreshed,
                    )
            except Exception as error:
                log.warning(
                    "telegram_customer_avatars_refresh_failed",
                    channel_id=str(channel.id),
                    error=str(error),
                )

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        await _handle_incoming_message(channel, client, event)

    @client.on(events.MessageRead(inbox=False))
    async def on_message_read(event: events.MessageRead.Event) -> None:
        if not event.outbox or event.chat_id is None or event.max_id is None:
            return
        async with SessionLocal() as session:
            await mark_mtproto_messages_read(
                session,
                channel.id,
                peer_id=int(event.chat_id),
                max_message_id=int(event.max_id),
            )

    receipt_task = asyncio.create_task(
        _poll_read_receipts(client, channel.id),
        name=f"telegram-read-receipts:{channel.id}",
    )
    try:
        await client.run_until_disconnected()
    finally:
        receipt_task.cancel()
        with suppress(asyncio.CancelledError):
            await receipt_task


async def _handle_incoming_message(
    channel: Channel,
    client: object,
    event: events.NewMessage.Event,
) -> None:
    text = str(event.raw_text or "").strip()
    # The current persistence contract stores a user access_hash, so only
    # one-to-one dialogs are supported. Groups/channels need a typed chat peer.
    if not text or event.is_channel or event.is_group:
        return
    input_chat = await event.get_input_chat()
    try:
        await client.send_read_acknowledge(
            input_chat,
            max_id=int(event.id),
            clear_mentions=True,
        )
    except Exception as error:
        log.warning(
            "telegram_inbound_read_ack_failed",
            channel_id=str(channel.id),
            message_id=str(event.id),
            error=str(error),
        )

    async with client.action(input_chat, "typing"):
        await _ingest_event(channel, client, event, text)


async def _ingest_event(
    channel: Channel,
    client: object,
    event: events.NewMessage.Event,
    text: str,
) -> None:
    sender = await event.get_sender()
    input_sender = await event.get_input_sender()
    sender_name = " ".join(
        part
        for part in (
            str(getattr(sender, "first_name", "") or "").strip(),
            str(getattr(sender, "last_name", "") or "").strip(),
        )
        if part
    ) or str(getattr(sender, "username", "") or event.sender_id or "Telegram")
    avatar_bytes: bytes | None = None
    avatar_checked = False
    try:
        if getattr(sender, "photo", None) is not None:
            downloaded = await client.download_profile_photo(sender, file=bytes)
            avatar_bytes = downloaded if isinstance(downloaded, bytes) else None
        avatar_checked = True
    except Exception as error:
        log.warning(
            "telegram_customer_avatar_download_failed",
            channel_id=str(channel.id),
            sender_id=str(event.sender_id),
            error=str(error),
        )
    async with SessionLocal() as session:
        live_channel = await session.get(Channel, channel.id)
        if live_channel is None or live_channel.status != "active":
            return
        await ingest_mtproto_message(
            session,
            live_channel,
            TelegramMTProtoInbound(
                peer_id=int(event.chat_id),
                peer_access_hash=getattr(input_sender, "access_hash", None),
                sender_id=int(event.sender_id or event.chat_id),
                message_id=int(event.id),
                text=text,
                sender_name=sender_name,
                avatar_bytes=avatar_bytes,
                avatar_checked=avatar_checked,
            ),
            auto_reply_delay_sec=max(
                0.0,
                settings.TELEGRAM_AUTO_REPLY_DELAY_SEC,
            ),
        )


async def _poll_read_receipts(client: object, channel_id: UUID) -> None:
    """Periodically reconcile durable dialog watermarks with local messages."""
    while True:
        try:
            async with SessionLocal() as session:
                changed = await reconcile_mtproto_messages_read(session, channel_id, client)
            if changed:
                log.info(
                    "telegram_read_receipts_reconciled",
                    channel_id=str(channel_id),
                    messages=changed,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning(
                "telegram_read_receipts_reconcile_failed",
                channel_id=str(channel_id),
                error=str(error),
            )
        await asyncio.sleep(5)


async def run_listener_loop() -> None:
    """Continuously discover connected accounts and keep one listener per channel.

    Account authorization happens in the API process after this worker may have
    started. A periodic refresh lets a newly connected account begin receiving
    messages without requiring an operator to restart the listener.
    """
    tasks: dict[UUID, asyncio.Task[None]] = {}
    try:
        while True:
            channels = await _active_channels()
            active_ids = {channel.id for channel in channels}

            for channel_id, task in list(tasks.items()):
                if channel_id not in active_ids:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    tasks.pop(channel_id, None)

            for channel in channels:
                channel_task = tasks.get(channel.id)
                if channel_task is not None and not channel_task.done():
                    continue
                if channel_task is not None:
                    try:
                        error = channel_task.exception()
                        if error is not None:
                            log.warning(
                                "telegram_listener_restarting",
                                channel_id=str(channel.id),
                                error=str(error),
                            )
                    except asyncio.CancelledError:
                        pass
                tasks[channel.id] = asyncio.create_task(
                    _run_channel(channel),
                    name=f"telegram-listener:{channel.id}",
                )

            await asyncio.sleep(5)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


async def main() -> None:
    """Run the standalone listener with an embedded-Qdrant-safe SQL fallback.

    A separate process cannot share embedded Qdrant's filesystem lock with the
    API. In-process mode calls ``run_listener_loop`` directly and therefore does
    not set this flag: API requests and Telegram ingestion then reuse the same
    cached vector-store client and both perform semantic top-4 retrieval.
    """
    os.environ.setdefault("AI_MANAGER_DISABLE_EMBEDDED_QDRANT", "1")
    try:
        await run_listener_loop()
    finally:
        await close_vector_stores()


async def _active_channels() -> list[Channel]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Channel).where(Channel.type == "telegram", Channel.status == "active")
        )
        return [
            channel
            for channel in result.scalars().all()
            if (channel.settings or {}).get("transport") == "mtproto"
        ]


if __name__ == "__main__":
    asyncio.run(main())
