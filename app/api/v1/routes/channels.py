"""Каналы: подключение и вебхуки. Экран: /channels. См. channel-integrations."""
from fastapi import APIRouter, Request

from app.api.deps import CurrentUser

router = APIRouter()


@router.get("")
async def list_channels(user: CurrentUser) -> list[dict]:
    return []  # TODO: подключённые каналы и статусы


@router.post("")
async def connect_channel(type: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: web (код виджета) | avito/vk (OAuth/токен)


@router.post("/webhook/{channel_type}")
async def webhook(channel_type: str, request: Request) -> dict:
    # TODO: проверить подпись → дедуп (webhook_event) → очередь обработки
    return {"ok": True}
