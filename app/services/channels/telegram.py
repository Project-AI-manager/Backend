"""Telegram channel adapter and local webhook processing."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.tenant import Tenant, TenantAIConfig
from app.schemas.channels import (
    ChannelConnectRequest,
    ChannelResponse,
    ChannelWebhookResponse,
)
from app.services.channels.base import ChannelAdapter, NormalizedMessage
from app.services.ml.contracts import AssistantProfile, ChatRole, ChatTurn, MLAnswerInput
from app.services.ml.memory import DatabaseMemoryRetriever
from app.services.ml.service import MLMessageService
from app.services.rag.llm import get_llm


class TelegramAdapter(ChannelAdapter):
    type = "telegram"

    def parse_inbound(self, payload: dict[str, Any]) -> NormalizedMessage:
        message = payload.get("message")
        if not isinstance(message, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Telegram message is required")

        chat = message.get("chat")
        sender = message.get("from") or {}
        if not isinstance(chat, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Telegram chat is required")
        text = str(message.get("text") or "").strip()
        if not text:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Telegram text message is required")

        update_id = payload.get("update_id")
        message_id = message.get("message_id")
        chat_id = chat.get("id")
        if update_id is None or message_id is None or chat_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Telegram update ids")

        customer_name = _telegram_customer_name(sender, chat)
        return NormalizedMessage(
            channel=self.type,
            external_conversation_id=str(chat_id),
            external_message_id=f"{update_id}:{message_id}",
            customer_ref=str(sender.get("id") or chat_id),
            customer_name=customer_name,
            text=text,
            attachments={},
        )

    async def send_outbound(self, conversation_ref: str, text: str) -> None:
        # Conversation-level replies use send_telegram_message because they need credentials.
        return None


async def send_telegram_message(channel: Channel, chat_id: str, text: str) -> bool:
    if not settings.TELEGRAM_DELIVERY_ENABLED:
        return False

    token = decrypt_secret(channel.credentials_encrypted)
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Telegram bot token is not configured")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=settings.TELEGRAM_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Telegram delivery failed",
        ) from exc

    return True


async def list_channels(
    session: AsyncSession,
    tenant_id: UUID,
) -> list[ChannelResponse]:
    result = await session.execute(
        select(Channel).where(Channel.tenant_id == tenant_id).order_by(Channel.created_at)
    )
    return [_channel_response(channel) for channel in result.scalars().all()]


async def connect_channel(
    session: AsyncSession,
    tenant_id: UUID,
    body: ChannelConnectRequest,
) -> ChannelResponse:
    if body.type != "telegram":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only Telegram is supported now")

    result = await session.execute(
        select(Channel).where(Channel.tenant_id == tenant_id, Channel.type == body.type)
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type=body.type)
        session.add(channel)

    channel.name = body.name.strip() or "Telegram"
    channel.status = "active"
    channel.credentials_encrypted = _store_bot_token(body.bot_token)
    webhook_secret = _webhook_secret(channel.settings)
    channel.settings = {
        "bot_username": body.bot_username.strip(),
        "webhook_path": f"/api/v1/channels/webhook/telegram/{webhook_secret}",
        "webhook_secret": webhook_secret,
    }
    await session.commit()
    await session.refresh(channel)
    return _channel_response(channel)


async def process_telegram_webhook(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    webhook_secret: str | None = None,
) -> ChannelWebhookResponse:
    adapter = TelegramAdapter()
    normalized = adapter.parse_inbound(payload)
    channel = await _active_telegram_channel(session, webhook_secret=webhook_secret)
    channel_id = channel.id

    event = WebhookEvent(
        channel_id=channel_id,
        external_event_id=str(payload["update_id"]),
        payload=payload,
        processed=False,
    )
    session.add(event)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel_id)

    customer = await _get_or_create_customer(session, channel, normalized)
    conversation = await _get_or_create_conversation(session, channel, customer, normalized)
    inbound = Message(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        sender_type="customer",
        sender_user_id=None,
        text=normalized.text,
        attachments=normalized.attachments,
        external_message_id=normalized.external_message_id,
        status="received",
        confidence=None,
        ai_meta={"source": "telegram", "chat_id": normalized.external_conversation_id},
    )
    session.add(inbound)
    await session.flush()

    tenant = await session.get(Tenant, channel.tenant_id)
    ai_config = await session.get(TenantAIConfig, channel.tenant_id)
    service = MLMessageService(
        retriever=DatabaseMemoryRetriever(session),
        llm=get_llm(ai_config.llm_provider if ai_config else "mock"),
    )
    history = await _conversation_history(session, conversation.id, exclude_message_id=inbound.id)
    answer = await service.answer(
        MLAnswerInput(
            tenant_id=channel.tenant_id,
            message=normalized.text,
            history=tuple(history),
            profile=AssistantProfile(company_name=tenant.name if tenant else "компания клиента"),
            custom_system_prompt=ai_config.system_prompt if ai_config else "",
            confidence_threshold=ai_config.confidence_threshold if ai_config else 80,
            auto_reply_enabled=ai_config.auto_reply_enabled if ai_config else False,
        )
    )

    outbound: Message | None = None
    if answer.decision == "auto_reply":
        outbound = Message(
            tenant_id=channel.tenant_id,
            conversation_id=conversation.id,
            direction="outbound",
            sender_type="ai",
            sender_user_id=None,
            text=answer.answer,
            attachments={},
            external_message_id=f"ai:{inbound.id}",
            status="sent",
            confidence=answer.confidence,
            ai_meta={
                "provider": answer.provider,
                "sources": [source.id for source in answer.sources],
                "delivery": "telegram-local-noop",
            },
        )
        session.add(outbound)
        conversation.status = "auto"
        delivered = await send_telegram_message(
            channel,
            normalized.external_conversation_id,
            answer.answer,
        )
        if delivered:
            outbound.ai_meta = {**outbound.ai_meta, "delivery": "telegram-bot-api"}
    else:
        conversation.status = "escalated"

    conversation.last_message_at = datetime.now(UTC)
    conversation.last_message_preview = normalized.text
    conversation.unread_count += 1
    event.processed = True
    await session.commit()
    await session.refresh(inbound)
    if outbound is not None:
        await session.refresh(outbound)

    return ChannelWebhookResponse(
        ok=True,
        duplicate=False,
        channel_id=channel.id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        outbound_message_id=outbound.id if outbound else None,
        decision=answer.decision,
    )


def _channel_response(channel: Channel) -> ChannelResponse:
    safe_settings = {
        key: value
        for key, value in (channel.settings or {}).items()
        if key not in {"bot_token", "webhook_secret"}
    }
    return ChannelResponse(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        status=channel.status,
        settings=safe_settings,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


def _store_bot_token(token: str) -> str:
    return encrypt_secret(token)


def _webhook_secret(settings: dict | None) -> str:
    existing = (settings or {}).get("webhook_secret")
    if isinstance(existing, str) and existing:
        return existing
    return secrets.token_urlsafe(24)


def _telegram_customer_name(sender: Any, chat: dict[str, Any]) -> str:
    if isinstance(sender, dict):
        first_name = str(sender.get("first_name") or "").strip()
        last_name = str(sender.get("last_name") or "").strip()
        username = str(sender.get("username") or "").strip()
        full_name = " ".join(part for part in (first_name, last_name) if part)
        if full_name:
            return full_name
        if username:
            return f"@{username}"
    return str(chat.get("title") or chat.get("id") or "Telegram customer")


async def _active_telegram_channel(
    session: AsyncSession,
    *,
    webhook_secret: str | None,
) -> Channel:
    result = await session.execute(
        select(Channel).where(Channel.type == "telegram", Channel.status == "active")
    )
    channels = result.scalars().all()
    if webhook_secret:
        for channel in channels:
            if _webhook_secret(channel.settings) == webhook_secret:
                return channel
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active Telegram channel not found")
    if len(channels) == 1:
        return channels[0]
    if not channels:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active Telegram channel not found")
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Telegram webhook secret is required")


async def _get_or_create_customer(
    session: AsyncSession,
    channel: Channel,
    message: NormalizedMessage,
) -> Customer:
    result = await session.execute(
        select(Customer, CustomerIdentity)
        .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
        .where(
            CustomerIdentity.channel_id == channel.id,
            CustomerIdentity.external_user_id == message.customer_ref,
        )
    )
    row = result.first()
    if row:
        customer, _ = row
        customer.display_name = message.customer_name or customer.display_name
        return customer

    customer = Customer(
        tenant_id=channel.tenant_id,
        display_name=message.customer_name or "Telegram customer",
        note="",
    )
    session.add(customer)
    await session.flush()
    session.add(
        CustomerIdentity(
            customer_id=customer.id,
            channel_id=channel.id,
            external_user_id=message.customer_ref,
        )
    )
    await session.flush()
    return customer


async def _get_or_create_conversation(
    session: AsyncSession,
    channel: Channel,
    customer: Customer,
    message: NormalizedMessage,
) -> Conversation:
    result = await session.execute(
        select(Conversation).where(
            Conversation.tenant_id == channel.tenant_id,
            Conversation.channel_id == channel.id,
            Conversation.customer_id == customer.id,
            Conversation.status.in_(("open", "auto", "escalated")),
        )
    )
    conversation = result.scalars().first()
    if conversation:
        return conversation

    conversation = Conversation(
        tenant_id=channel.tenant_id,
        customer_id=customer.id,
        channel_id=channel.id,
        status="open",
        assignee_user_id=None,
        last_message_at=datetime.now(UTC),
        last_message_preview=message.text,
        unread_count=0,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _conversation_history(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    exclude_message_id: UUID,
) -> list[ChatTurn]:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.id != exclude_message_id)
        .order_by(Message.created_at)
    )
    turns: list[ChatTurn] = []
    for message in result.scalars().all():
        role: ChatRole = "customer" if message.sender_type == "customer" else "manager"
        if message.sender_type == "ai":
            role = "ai"
        turns.append(ChatTurn(role=role, text=message.text))
    return turns
