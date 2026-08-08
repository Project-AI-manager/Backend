"""Conversation read services for the inbox."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import case, desc, select
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
from app.services.channels.avito import send_avito_message
from app.services.channels.telegram import send_telegram_file, send_telegram_message
from app.services.channels.telegram_mtproto import (
    apply_mtproto_read_watermark,
    send_mtproto_file,
    send_mtproto_message,
)
from app.services.channels.vk import VK_MAX_MESSAGE_LENGTH, send_vk_message
from app.services.channels.whatsapp import send_whatsapp_message
from app.services.conversation_attachments import (
    StoredConversationAttachment,
    attachment_path,
    delete_attachment,
)
from app.services.customer_avatars import customer_avatar_exists, get_customer_avatar
from app.services.escalation_notifications import notify_escalation_if_due


async def list_conversations(
    session: AsyncSession,
    tenant_id: UUID,
    status_filter: str | None = None,
) -> list[ConversationResponse]:
    query = (
        select(Conversation, Customer.display_name, Channel.type)
        .join(Customer, Customer.id == Conversation.customer_id)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(Conversation.tenant_id == tenant_id)
    )
    if status_filter:
        query = query.where(Conversation.status == status_filter)
    result = await session.execute(query.order_by(desc(Conversation.last_message_at)))
    return [
        _conversation_response(conversation, customer_name, channel_type)
        for conversation, customer_name, channel_type in result.all()
    ]


async def get_conversation_thread(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> ConversationThreadResponse:
    result = await session.execute(
        select(Conversation, Customer.display_name, Channel.type)
        .join(Customer, Customer.id == Conversation.customer_id)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    conversation, customer_name, channel_type = row
    messages_result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.tenant_id == tenant_id)
        .order_by(
            Message.created_at,
            case((Message.direction == "inbound", 0), else_=1),
            Message.id,
        )
    )
    base = _conversation_response(conversation, customer_name, channel_type)
    return ConversationThreadResponse(
        **base.model_dump(),
        messages=[_message_response(message) for message in messages_result.scalars().all()],
    )


async def mark_conversation_read(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> ConversationThreadResponse:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.id == conversation_id, Conversation.tenant_id == tenant_id)
        .with_for_update()
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    conversation.unread_count = 0
    await session.commit()
    return await get_conversation_thread(session, tenant_id, conversation_id)


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

    replied_at = datetime.now(UTC)
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
        created_at=replied_at,
        ai_meta={
            "source": "manager",
            **({"chat_id": chat_id} if chat_id else {}),
            **(
                {"peer_access_hash": latest_inbound.ai_meta["peer_access_hash"]}
                if latest_inbound
                and isinstance((latest_inbound.ai_meta or {}).get("peer_access_hash"), int)
                else {}
            ),
        },
    )
    session.add(message)
    await session.flush()

    if channel.type == "vk" and len(text) > VK_MAX_MESSAGE_LENGTH:
        message.status = "failed"
        message.ai_meta = {**message.ai_meta, "delivery": "vk-message-too-long"}
        await session.commit()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"VK message exceeds {VK_MAX_MESSAGE_LENGTH} characters",
        )

    if channel.type == "vk":
        # Make the outbound id/random_id durable before the provider call.
        await session.commit()

    try:
        delivered, read = await _deliver_outbound_message(channel, message)
    except HTTPException:
        if channel.type == "vk":
            message = await session.get(Message, message.id)
            if message is not None:
                message.status = "pending"
                message.ai_meta = {**(message.ai_meta or {}), "delivery": "vk-delivery-unknown"}
                await session.commit()
        raise
    transport = str((channel.settings or {}).get("transport") or "")
    message.status = "sent" if delivered else "failed" if transport == "mtproto" else "pending"
    if delivered and read:
        message.status = "read"
    message.ai_meta = {
        **message.ai_meta,
        "delivery": (
            "channel-sent"
            if delivered
            else "telegram-mtproto-failed"
            if transport == "mtproto"
            else "delivery-disabled"
        ),
    }

    conversation.status = "answered"
    conversation.last_message_at = replied_at
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
    if message.status == "sent" and transport == "mtproto":
        await apply_mtproto_read_watermark(session, channel.id, message)
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
    was_escalated = conversation.status == "escalated"
    conversation.status = "escalated"
    conversation.assignee_user_id = user_id
    conversation.last_message_at = conversation.last_message_at or datetime.now(UTC)

    if not was_escalated:
        latest_inbound = await _latest_inbound_message(session, tenant_id, conversation_id)
        message_preview = (
            latest_inbound.text
            if latest_inbound and latest_inbound.text.strip()
            else conversation.last_message_preview
        )
        await notify_escalation_if_due(session, conversation, message_preview)

    await session.commit()
    thread = await get_conversation_thread(session, tenant_id, conversation.id)
    return ConversationActionResponse(conversation=thread, message=None, delivered=None)


async def reply_to_conversation_with_file(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
    user_id: UUID,
    text: str,
    attachments: list[StoredConversationAttachment],
) -> ConversationActionResponse:
    caption = text.strip()
    if not attachments:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Добавьте хотя бы один файл")
    conversation, _customer_name, channel = await _conversation_with_channel(
        session, tenant_id, conversation_id
    )
    latest_inbound = await _latest_inbound_message(session, tenant_id, conversation_id)
    chat_id = _message_chat_id(latest_inbound)
    replied_at = datetime.now(UTC)
    metadata_items = [dict(attachment.metadata) for attachment in attachments]
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        direction="outbound",
        sender_type="manager",
        sender_user_id=user_id,
        text=caption,
        attachments={"items": [dict(metadata) for metadata in metadata_items]},
        external_message_id=None,
        status="pending",
        confidence=None,
        created_at=replied_at,
        ai_meta={
            "source": "manager",
            **({"chat_id": chat_id} if chat_id else {}),
            **(
                {"peer_access_hash": latest_inbound.ai_meta["peer_access_hash"]}
                if latest_inbound
                and isinstance((latest_inbound.ai_meta or {}).get("peer_access_hash"), int)
                else {}
            ),
        },
    )
    session.add(message)
    try:
        await session.flush()
        delivered, telegram_message_ids, read = await _deliver_outbound_attachments(
            channel,
            message,
            [attachment.path for attachment in attachments],
            metadata_items,
        )
        transport = str((channel.settings or {}).get("transport") or "")
        message.status = "sent" if delivered else "failed" if transport == "mtproto" else "pending"
        if delivered and read:
            message.status = "read"
        sent_message_ids = [message_id for message_id in telegram_message_ids if message_id]
        if sent_message_ids:
            first_message_id = sent_message_ids[0]
            message.external_message_id = f"telegram:{chat_id}:{first_message_id}"
            message.ai_meta = {
                **message.ai_meta,
                "telegram_message_id": first_message_id,
                "telegram_message_ids": sent_message_ids,
            }
            for metadata, telegram_message_id in zip(
                metadata_items, telegram_message_ids, strict=False
            ):
                if telegram_message_id is not None:
                    metadata["telegram_message_id"] = telegram_message_id
            message.attachments = {"items": metadata_items}
        message.ai_meta = {
            **message.ai_meta,
            "delivery": "channel-sent" if delivered else "delivery-disabled",
        }
        conversation.status = "answered"
        conversation.last_message_at = replied_at
        conversation.last_message_preview = caption[:512] or str(metadata_items[0]["name"])
        conversation.unread_count = 0
        await session.commit()
        if message.status == "sent" and transport == "mtproto":
            await apply_mtproto_read_watermark(session, channel.id, message)
        await session.refresh(message)
    except Exception:
        await session.rollback()
        for attachment in attachments:
            delete_attachment(attachment.path)
        raise
    thread = await get_conversation_thread(session, tenant_id, conversation.id)
    return ConversationActionResponse(
        conversation=thread,
        message=_message_response(message),
        delivered=delivered,
    )


async def close_conversation(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> ConversationActionResponse:
    conversation, _customer_name, _channel = await _conversation_with_channel(
        session,
        tenant_id,
        conversation_id,
    )
    conversation.status = "closed"
    conversation.unread_count = 0

    await session.commit()
    thread = await get_conversation_thread(session, tenant_id, conversation.id)
    return ConversationActionResponse(conversation=thread, message=None, delivered=None)


def _conversation_response(
    conversation: Conversation,
    customer_name: str,
    channel_type: str,
) -> ConversationResponse:
    return ConversationResponse(
        id=conversation.id,
        channel_id=conversation.channel_id,
        channel_type=channel_type,
        customer_id=conversation.customer_id,
        customer_name=customer_name,
        avatar_url=(
            f"/api/v1/conversations/{conversation.id}/avatar"
            if customer_avatar_exists(conversation.tenant_id, conversation.customer_id)
            else None
        ),
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
        sender_user_id=message.sender_user_id,
        text=message.text,
        attachments=_public_attachments(message),
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
) -> tuple[bool, bool]:
    if channel.type == "whatsapp":
        chat_id = _message_chat_id(message)
        if not chat_id:
            return False, False
        result = await send_whatsapp_message(channel, chat_id, message.text)
        if result.external_message_id:
            message.external_message_id = result.external_message_id
        message.ai_meta = {**(message.ai_meta or {}), **result.metadata}
        return result.delivered, result.status == "read"
    if channel.type == "avito":
        chat_id = _message_chat_id(message)
        if not chat_id:
            return False, False
        result = await send_avito_message(channel, chat_id, message.text)
        if result.external_message_id:
            message.external_message_id = result.external_message_id
        message.ai_meta = {**(message.ai_meta or {}), **result.metadata}
        return result.delivered, False
    if channel.type == "vk":
        chat_id = _message_chat_id(message)
        if not chat_id:
            return False, False
        result = await send_vk_message(
            channel,
            chat_id,
            message.text,
            idempotency_key=str(message.id),
        )
        if result.external_message_id:
            message.external_message_id = result.external_message_id
        message.ai_meta = {**(message.ai_meta or {}), **result.metadata}
        return result.delivered, False
    if channel.type != "telegram":
        return False, False

    chat_id = _message_chat_id(message)
    if not chat_id:
        return False, False

    transport = str((channel.settings or {}).get("transport") or "")
    if transport == "mtproto":
        raw_access_hash = (message.ai_meta or {}).get("peer_access_hash")
        access_hash = raw_access_hash if isinstance(raw_access_hash, int) else None
        delivery = await send_mtproto_message(
            channel,
            chat_id,
            message.text,
            peer_access_hash=access_hash,
        )
        delivered = delivery.delivered if hasattr(delivery, "delivered") else bool(delivery)
        telegram_message_id = getattr(delivery, "message_id", None)
        if delivered and telegram_message_id is not None:
            message.external_message_id = f"mtproto:{chat_id}:{telegram_message_id}"
            message.ai_meta = {
                **message.ai_meta,
                "telegram_message_id": telegram_message_id,
            }
        return delivered, bool(getattr(delivery, "read", False))

    delivered = await send_telegram_message(channel, chat_id, message.text)
    message.external_message_id = f"manager:{message.id}"
    return delivered, False


async def _deliver_outbound_attachment(
    channel: Channel,
    message: Message,
    file_path: Path,
    metadata: dict[str, object],
) -> tuple[bool, int | None, bool]:
    if channel.type != "telegram":
        return False, None, False
    chat_id = _message_chat_id(message)
    if not chat_id:
        return False, None, False
    is_image = metadata.get("kind") == "image"
    if (channel.settings or {}).get("transport") == "mtproto":
        raw_hash = (message.ai_meta or {}).get("peer_access_hash")
        delivery = await send_mtproto_file(
            channel,
            chat_id,
            str(file_path),
            message.text,
            peer_access_hash=raw_hash if isinstance(raw_hash, int) else None,
            force_document=not is_image,
        )
        return (
            delivery.delivered,
            delivery.message_id,
            bool(getattr(delivery, "read", False)),
        )
    delivered, telegram_message_id = await send_telegram_file(
        channel, chat_id, str(file_path), message.text, is_image=is_image
    )
    return delivered, telegram_message_id, False


async def _deliver_outbound_attachments(
    channel: Channel,
    message: Message,
    file_paths: list[Path],
    metadata_items: list[dict[str, object]],
) -> tuple[bool, list[int | None], bool]:
    deliveries: list[bool] = []
    message_ids: list[int | None] = []
    read_states: list[bool] = []
    original_text = message.text
    try:
        for index, (file_path, metadata) in enumerate(zip(file_paths, metadata_items, strict=True)):
            # Telegram captions belong to an individual media item. Send the
            # user's text once, below the ordered attachment group in our UI.
            message.text = original_text if index == 0 else ""
            delivered, message_id, read = await _deliver_outbound_attachment(
                channel, message, file_path, metadata
            )
            deliveries.append(delivered)
            message_ids.append(message_id)
            read_states.append(read)
    finally:
        message.text = original_text
    return all(deliveries), message_ids, bool(read_states) and all(read_states)


def _public_attachments(message: Message) -> dict:
    raw_items = (message.attachments or {}).get("items")
    if not isinstance(raw_items, list):
        return {}
    items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        public = {key: value for key, value in raw.items() if key != "storage_key"}
        public["download_url"] = (
            f"/api/v1/conversations/{message.conversation_id}/attachments/{raw['id']}"
        )
        items.append(public)
    return {"items": items}


async def get_conversation_attachment(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
    attachment_id: UUID,
) -> tuple[Path, str, str]:
    result = await session.execute(
        select(Message).where(
            Message.tenant_id == tenant_id,
            Message.conversation_id == conversation_id,
        )
    )
    for message in result.scalars().all():
        raw_items = (message.attachments or {}).get("items")
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            if not isinstance(item, dict) or str(item.get("id")) != str(attachment_id):
                continue
            storage_key = item.get("storage_key")
            if not isinstance(storage_key, str):
                break
            path = attachment_path(storage_key)
            if not path.is_file():
                break
            return (
                path,
                str(item.get("name") or "attachment"),
                str(item.get("content_type") or "application/octet-stream"),
            )
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Вложение не найдено")


async def get_conversation_avatar(
    session: AsyncSession,
    tenant_id: UUID,
    conversation_id: UUID,
) -> tuple[Path, str]:
    result = await session.execute(
        select(Conversation.customer_id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant_id,
            Customer.tenant_id == tenant_id,
            Channel.tenant_id == tenant_id,
        )
    )
    customer_id = result.scalar_one_or_none()
    if customer_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Аватар не найден")
    avatar = get_customer_avatar(tenant_id, customer_id)
    if avatar is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Аватар не найден")
    return avatar


def _message_chat_id(message: Message | None) -> str | None:
    if message is None:
        return None
    raw_chat_id = message.ai_meta.get("chat_id")
    if isinstance(raw_chat_id, str) and raw_chat_id:
        return raw_chat_id
    if isinstance(raw_chat_id, int):
        return str(raw_chat_id)
    return None
