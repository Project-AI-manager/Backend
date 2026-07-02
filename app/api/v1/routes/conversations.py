"""Inbox conversations and messages. Screen: /inbox."""

import uuid

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.schemas.conversations import (
    ConversationActionResponse,
    ConversationReplyRequest,
    ConversationResponse,
    ConversationThreadResponse,
)
from app.services.conversations import (
    escalate_conversation,
    get_conversation_thread,
    list_conversations,
    reply_to_conversation,
)

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


@router.post("/{conversation_id}/reply", response_model=ConversationActionResponse)
async def reply(
    conversation_id: uuid.UUID,
    body: ConversationReplyRequest,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationActionResponse:
    return await reply_to_conversation(
        session,
        tenant_id_from_user(user),
        conversation_id,
        uuid.UUID(str(user["sub"])),
        body,
    )


@router.post("/{conversation_id}/escalate", response_model=ConversationActionResponse)
async def escalate(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationActionResponse:
    return await escalate_conversation(
        session,
        tenant_id_from_user(user),
        conversation_id,
        uuid.UUID(str(user["sub"])),
    )
