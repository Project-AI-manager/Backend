"""Inbox conversations and messages. Screen: /inbox."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.core.config import settings
from app.schemas.conversations import (
    ConversationActionResponse,
    ConversationReplyRequest,
    ConversationResponse,
    ConversationThreadResponse,
)
from app.services.conversation_attachments import (
    delete_attachment,
    validate_and_store_attachment,
)
from app.services.conversations import (
    close_conversation,
    escalate_conversation,
    get_conversation_attachment,
    get_conversation_thread,
    list_conversations,
    mark_conversation_read,
    reply_to_conversation,
    reply_to_conversation_with_file,
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


@router.post("/{conversation_id}/read", response_model=ConversationThreadResponse)
async def mark_read(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationThreadResponse:
    return await mark_conversation_read(
        session,
        tenant_id_from_user(user),
        conversation_id,
    )


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


@router.post("/{conversation_id}/reply-with-file", response_model=ConversationActionResponse)
async def reply_with_file(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    text: Annotated[str, Form(max_length=1024)] = "",
) -> ConversationActionResponse:
    tenant_id = tenant_id_from_user(user)
    data = await file.read(settings.CONVERSATION_ATTACHMENT_MAX_BYTES + 1)
    stored = validate_and_store_attachment(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        filename=file.filename or "",
        content_type=file.content_type,
        data=data,
    )
    try:
        return await reply_to_conversation_with_file(
            session,
            tenant_id,
            conversation_id,
            uuid.UUID(str(user["sub"])),
            text,
            stored,
        )
    except Exception:
        delete_attachment(stored.path)
        raise


@router.get("/{conversation_id}/attachments/{attachment_id}", response_class=FileResponse)
async def download_attachment(
    conversation_id: uuid.UUID,
    attachment_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> FileResponse:
    path, filename, content_type = await get_conversation_attachment(
        session,
        tenant_id_from_user(user),
        conversation_id,
        attachment_id,
    )
    return FileResponse(
        path,
        filename=filename,
        media_type=content_type,
        headers={"X-Content-Type-Options": "nosniff"},
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


@router.post("/{conversation_id}/close", response_model=ConversationActionResponse)
async def close(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> ConversationActionResponse:
    return await close_conversation(
        session,
        tenant_id_from_user(user),
        conversation_id,
    )
