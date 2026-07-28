"""Long-running Telegram MTProto listener for all active personal-account channels.

Run separately from the API: ``python -m app.workers.telegram_listener``.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import UUID

from sqlalchemy import select
from telethon import events  # type: ignore[import-untyped]

from app.core.logging import log
from app.db.session import SessionLocal
from app.models.channel import Channel
from app.schemas.channels import TelegramMTProtoInbound
from app.services.channels.telegram_mtproto import (
    create_authorized_client,
    ingest_mtproto_message,
    mark_mtproto_messages_read,
)


async def _run_channel(channel: Channel) -> None:
    client = await create_authorized_client(channel)

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        text = str(event.raw_text or "").strip()
        # The current persistence contract stores a user access_hash, so only
        # one-to-one dialogs are supported. Groups/channels need a typed chat peer.
        if not text or event.is_channel or event.is_group:
            return
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
                ),
            )

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

    await client.run_until_disconnected()


async def main() -> None:
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
                task = tasks.get(channel.id)
                if task is not None and not task.done():
                    continue
                if task is not None:
                    try:
                        error = task.exception()
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
