"""Диалоги и сообщения — ядро inbox. Экран: /inbox, /inbox/[id]."""
from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter()


@router.get("")
async def list_conversations(user: CurrentUser, status: str | None = None) -> list[dict]:
    return []  # TODO: лента диалогов с фильтрами (auto|escalated|mine)


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: тред сообщений + контекст клиента


@router.post("/{conversation_id}/reply")
async def reply(conversation_id: str, text: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: ответ менеджера → отправка в канал + kb_candidate


@router.post("/{conversation_id}/escalate")
async def escalate(conversation_id: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: ручная эскалация
