"""Conversation read services for the inbox."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel
from app.models.conversation import Conversation, Customer, Message
from app.models.knowledge import KbCandidate
from app.schemas.conversations import (
    ConversationActionResponse,
    ConversationMessageResponse,
    ConversationReplyRequest,
    ConversationResponse,
    ConversationThreadResponse,
)
from app.services.channels.telegram import send_telegram_message


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


async def reply_to_conversation(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
    user_id: UUID,
    body: ConversationReplyRequest,
) -> ConversationActionResponse:
    text = body.text.strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Reply text is required")

    conversation, _customer_name, channel = await _conversation_with_channel(
        session,
        tenant_id,
        conversation_id,
    )
    latest_inbound = await _latest_inbound_message(session, tenant_id, conversation_id)
    chat_id = _message_chat_id(latest_inbound)

    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        direction="outbound",
        sender_type="manager",
        sender_user_id=user_id,
        text=text,
        attachments={},
        external_message_id=None,
        status="pending",
        confidence=None,
        ai_meta={
            "source": "manager",
            **({"chat_id": chat_id} if chat_id else {}),
        },
    )
    session.add(message)
    await session.flush()

    delivered = await _deliver_outbound_message(channel, message)
    message.status = "sent" if delivered else "pending"
    message.ai_meta = {
        **message.ai_meta,
        "delivery": "channel-sent" if delivered else "delivery-disabled",
    }

    conversation.status = "open"
    conversation.last_message_at = datetime.now(UTC)
    conversation.last_message_preview = text[:512]
    conversation.unread_count = 0

    if latest_inbound and latest_inbound.text.strip():
        session.add(
            KbCandidate(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                question=latest_inbound.text,
                answer=text,
                suggested_by="manager",
                status="pending",
                resulting_document_id=None,
            )
        )

    await session.commit()
    await session.refresh(message)
    thread = await get_conversation_thread(session, tenant_id, conversation.id)
    return ConversationActionResponse(
        conversation=thread,
        message=_message_response(message),
        delivered=delivered,
    )


async def escalate_conversation(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
    user_id: UUID,
) -> ConversationActionResponse:
    conversation, _customer_name, _channel = await _conversation_with_channel(
        session,
        tenant_id,
        conversation_id,
    )
    conversation.status = "escalated"
    conversation.assignee_user_id = user_id
    conversation.last_message_at = conversation.last_message_at or datetime.now(UTC)

    await session.commit()
    thread = await get_conversation_thread(session, tenant_id, conversation.id)
    return ConversationActionResponse(conversation=thread, message=None, delivered=None)


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


async def _conversation_with_channel(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> tuple[Conversation, str, Channel]:
    result = await session.execute(
        select(Conversation, Customer.display_name, Channel)
        .join(Customer, Customer.id == Conversation.customer_id)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    conversation, customer_name, channel = row
    return conversation, customer_name, channel


async def _latest_inbound_message(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> Message | None:
    result = await session.execute(
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
        )
        .order_by(desc(Message.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _deliver_outbound_message(
    channel: Channel,
    message: Message,
) -> bool:
    if channel.type != "telegram":
        return False

    chat_id = _message_chat_id(message)
    if not chat_id:
        return False

    delivered = await send_telegram_message(channel, chat_id, message.text)
    message.external_message_id = f"manager:{message.id}"
    return delivered


def _message_chat_id(message: Message | None) -> str | None:
    if message is None:
        return None
    raw_chat_id = message.ai_meta.get("chat_id")
    if isinstance(raw_chat_id, str) and raw_chat_id:
        return raw_chat_id
    if isinstance(raw_chat_id, int):
        return str(raw_chat_id)
    return None
