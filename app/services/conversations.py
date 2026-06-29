"""Conversation read services for the inbox."""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Customer, Message
from app.schemas.conversations import (
    ConversationMessageResponse,
    ConversationResponse,
    ConversationThreadResponse,
)


async def list_conversations(
    session: AsyncSession,
    tenant_id: UUID,
    status_filter: str | None = None,
) -> list[ConversationResponse]:
    query = (
        select(Conversation, Customer.display_name)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.tenant_id == tenant_id)
    )
    if status_filter:
        query = query.where(Conversation.status == status_filter)
    result = await session.execute(query.order_by(desc(Conversation.last_message_at)))
    return [
        _conversation_response(conversation, customer_name)
        for conversation, customer_name in result.all()
    ]


async def get_conversation_thread(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> ConversationThreadResponse:
    result = await session.execute(
        select(Conversation, Customer.display_name)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    conversation, customer_name = row
    messages_result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.tenant_id == tenant_id)
        .order_by(Message.created_at)
    )
    base = _conversation_response(conversation, customer_name)
    return ConversationThreadResponse(
        **base.model_dump(),
        messages=[_message_response(message) for message in messages_result.scalars().all()],
    )


def _conversation_response(
    conversation: Conversation,
    customer_name: str,
) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        channel_id=conversation.channel_id,
        customer_id=conversation.customer_id,
        customer_name=customer_name,
        status=conversation.status,
        last_message_at=conversation.last_message_at,
        last_message_preview=conversation.last_message_preview,
        unread_count=conversation.unread_count,
    )


def _message_response(message: Message) -> ConversationMessageResponse:
    return ConversationMessageResponse(
        id=message.id,
        direction=message.direction,
        sender_type=message.sender_type,
        text=message.text,
        status=message.status,
        confidence=message.confidence,
        ai_meta=message.ai_meta,
        created_at=message.created_at,
    )
