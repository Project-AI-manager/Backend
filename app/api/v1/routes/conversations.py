"""Диалоги и сообщения — ядро inbox. Экран: /inbox, /inbox/[id]."""

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.schemas.conversations import ConversationResponse, ConversationThreadResponse
from app.services.conversations import get_conversation_thread, list_conversations

router = APIRouter()


@router.get("", response_model=list[ConversationResponse])
async def list_conversation_items(
    user: CurrentUser,
    session: SessionDep,
    status: str | None = None,
) -> list[ConversationResponse]:
    return await list_conversations(session, tenant_id_from_user(user), status)


@router.get("/{conversation_id}", response_model=ConversationThreadResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationThreadResponse:
    return await get_conversation_thread(session, tenant_id_from_user(user), conversation_id)


@router.post("/{conversation_id}/reply")
async def reply(conversation_id: str, text: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: ответ менеджера → отправка в канал + kb_candidate


@router.post("/{conversation_id}/escalate")
async def escalate(conversation_id: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: ручная эскалация
