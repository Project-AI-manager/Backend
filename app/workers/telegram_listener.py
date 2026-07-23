"""Long-running Telegram MTProto listener for all active personal-account channels.

Run separately from the API: ``python -m app.workers.telegram_listener``.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from telethon import events  # type: ignore[import-untyped]

from app.db.session import SessionLocal
from app.models.channel import Channel
from app.schemas.channels import TelegramMTProtoInbound
from app.services.channels.telegram_mtproto import create_authorized_client, ingest_mtproto_message


async def _run_channel(channel: Channel) -> None:
    client = await create_authorized_client(channel)

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        text = str(event.raw_text or "").strip()
        if not text or event.is_channel:
            return
        sender = await event.get_sender()
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
                    sender_id=int(event.sender_id or event.chat_id),
                    message_id=int(event.id),
                    text=text,
                    sender_name=sender_name,
                ),
            )

    await client.run_until_disconnected()


async def main() -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Channel).where(Channel.type == "telegram", Channel.status == "active")
        )
        channels = [
            channel
            for channel in result.scalars().all()
            if (channel.settings or {}).get("transport") == "mtproto"
        ]
    if not channels:
        raise RuntimeError("No active Telegram MTProto channels")
    await asyncio.gather(*(_run_channel(channel) for channel in channels))


if __name__ == "__main__":
    asyncio.run(main())
