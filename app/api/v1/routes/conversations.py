"""Inbox conversations and messages. Screen: /inbox."""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
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
    get_conversation_avatar,
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


@router.get("/{conversation_id}/avatar", response_class=FileResponse)
async def customer_avatar(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> FileResponse:
    path, content_type = await get_conversation_avatar(
        session,
        tenant_id_from_user(user),
        conversation_id,
    )
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
    file: Annotated[list[UploadFile] | None, File()] = None,
    files: Annotated[list[UploadFile] | None, File()] = None,
    bracket_files: Annotated[list[UploadFile] | None, File(alias="files[]")] = None,
    text: Annotated[str, Form(max_length=1024)] = "",
) -> ConversationActionResponse:
    tenant_id = tenant_id_from_user(user)
    uploads = (file or []) + (files or []) + (bracket_files or [])
    if not uploads:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Добавьте хотя бы один файл")
    if len(uploads) > 10:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "За одно сообщение можно прикрепить не более 10 файлов",
        )

    stored = []
    try:
        for upload in uploads:
            data = await upload.read(settings.CONVERSATION_ATTACHMENT_MAX_BYTES + 1)
            stored.append(
                validate_and_store_attachment(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    filename=upload.filename or "",
                    content_type=upload.content_type,
                    data=data,
                )
            )
        return await reply_to_conversation_with_file(
            session,
            tenant_id,
            conversation_id,
            uuid.UUID(str(user["sub"])),
            text,
            stored,
        )
    except Exception:
        for attachment in stored:
            delete_attachment(attachment.path)
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
