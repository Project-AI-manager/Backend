"""MAX Bot API channel integration."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.schemas.channels import ChannelResponse, ChannelWebhookResponse, MaxConnectRequest
from app.services.channels.base import DeliveryResult, NormalizedMessage

MAX_MAX_MESSAGE_LENGTH = 4000
MAX_PROCESSING_LEASE = timedelta(minutes=5)


async def connect_max_channel(
    session: AsyncSession,
    tenant_id: UUID,
    body: MaxConnectRequest,
) -> ChannelResponse:
    replacing = (
        await _owned_channel(session, tenant_id, body.replace_channel_id)
        if body.replace_channel_id is not None
        else None
    )
    bot = await _max_call("GET", "/me", body.bot_token)
    bot_id = str(bot.get("user_id") or "")
    if not bot_id or bot.get("is_bot") is not True:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid MAX bot response")

    external_identity = f"max:{bot_id}"
    matching = (
        await session.execute(select(Channel).where(Channel.external_identity == external_identity))
    ).scalar_one_or_none()
    if matching is not None and matching.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This MAX bot is already connected")
    if replacing is not None and matching is not None and matching.id != replacing.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This MAX bot is already connected")
    if (
        replacing is not None
        and replacing.external_identity
        and replacing.external_identity != external_identity
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Replace can only rotate credentials for the same MAX bot",
        )

    channel = replacing or matching
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type="max")
        session.add(channel)
    channel.type = "max"
    channel.name = body.name.strip() or str(bot.get("first_name") or "MAX")
    channel.status = "active"
    channel.external_identity = external_identity
    webhook_secret = secrets.token_urlsafe(32)
    channel.credentials_encrypted = encrypt_secret(
        json.dumps(
            {"bot_token": body.bot_token, "webhook_secret": webhook_secret},
            separators=(",", ":"),
        )
    )
    channel.settings = {
        "bot_id": bot_id,
        "username": str(bot.get("username") or ""),
        "display_name": str(bot.get("first_name") or bot.get("name") or ""),
    }
    await session.flush()
    webhook_url = (
        f"{settings.API_PUBLIC_URL.rstrip('/')}/api/v1/channels/webhook/max/{channel.id}"
    )
    try:
        await _max_call(
            "POST",
            "/subscriptions",
            body.bot_token,
            json_body={
                "url": webhook_url,
                "update_types": ["message_created", "bot_started"],
                "secret": webhook_secret,
            },
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "This MAX bot is already connected") from exc
    except HTTPException:
        await session.rollback()
        raise
    await session.refresh(channel)
    return _channel_response(channel)


async def process_max_webhook(
    session: AsyncSession,
    channel_id: UUID,
    payload: dict[str, Any],
    signature: str | None,
) -> ChannelWebhookResponse:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.type != "max" or channel.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active MAX channel not found")
    credentials = _credentials(channel)
    if not signature or not secrets.compare_digest(signature, credentials["webhook_secret"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid MAX webhook secret")
    if payload.get("update_type") != "message_created":
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)

    message = payload.get("message")
    if not isinstance(message, dict):
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    sender = message.get("sender")
    body = message.get("body")
    recipient = message.get("recipient")
    if not isinstance(sender, dict) or not isinstance(body, dict):
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    sender_id = str(sender.get("user_id") or "")
    if not sender_id or sender.get("is_bot") is True:
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    if isinstance(recipient, dict):
        recipient_id = str(recipient.get("user_id") or "")
        if recipient_id and recipient_id != str((channel.settings or {}).get("bot_id") or ""):
            return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    message_id = str(body.get("mid") or message.get("mid") or "")
    text = str(body.get("text") or "").strip()
    if not message_id or not text:
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)

    existing = await session.execute(
        select(WebhookEvent).where(
            WebhookEvent.channel_id == channel.id,
            WebhookEvent.external_event_id == message_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)

    event = WebhookEvent(
        channel_id=channel.id,
        external_event_id=message_id,
        payload={"type": "message_created", "message_id": message_id, "sender_id": sender_id},
        processed=False,
    )
    session.add(event)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)

    customer_name = " ".join(
        part
        for part in (str(sender.get("first_name") or ""), str(sender.get("last_name") or ""))
        if part
    )
    normalized = NormalizedMessage(
        channel="max",
        external_conversation_id=sender_id,
        external_message_id=message_id,
        customer_ref=sender_id,
        customer_name=customer_name or f"MAX {sender_id}",
        text=text,
    )
    customer = await _get_or_create_customer(session, channel, normalized)
    conversation = await _get_or_create_conversation(session, channel, customer, normalized)
    inbound = Message(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        sender_type="customer",
        text=text,
        attachments={},
        external_message_id=message_id,
        status="received",
        ai_meta={"source": "max", "chat_id": sender_id, "webhook_event_id": message_id},
    )
    session.add(inbound)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)
    await session.commit()
    return ChannelWebhookResponse(
        ok=True,
        channel_id=channel.id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        processed_count=1,
    )


async def send_max_message(channel: Channel, user_id: str, text: str) -> DeliveryResult:
    if len(text) > MAX_MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"MAX message exceeds {MAX_MAX_MESSAGE_LENGTH} characters",
        )
    payload = await _max_call(
        "POST",
        "/messages",
        _credentials(channel)["bot_token"],
        params={"user_id": user_id},
        json_body={"text": text},
    )
    raw_message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    body = raw_message.get("body") if isinstance(raw_message, dict) else None
    message_id = str(
        (body.get("mid") if isinstance(body, dict) else None)
        or (raw_message.get("mid") if isinstance(raw_message, dict) else None)
        or ""
    )
    return DeliveryResult(
        delivered=bool(message_id),
        external_message_id=message_id or None,
        status="sent" if message_id else "failed",
        metadata={"delivery": "max-bot-api"},
    )


async def unsubscribe_max_webhook(channel: Channel) -> None:
    if not channel.credentials_encrypted:
        return
    webhook_url = (
        f"{settings.API_PUBLIC_URL.rstrip('/')}/api/v1/channels/webhook/max/{channel.id}"
    )
    await _max_call(
        "DELETE",
        "/subscriptions",
        _credentials(channel)["bot_token"],
        params={"url": webhook_url},
    )


async def process_pending_max(session: AsyncSession) -> dict[str, int]:
    from app.services.channels.telegram import process_channel_inbound_message

    stale_before = datetime.now(UTC) - MAX_PROCESSING_LEASE
    query = (
        select(WebhookEvent.id)
        .join(Channel, Channel.id == WebhookEvent.channel_id)
        .join(Conversation, Conversation.channel_id == Channel.id)
        .join(
            Message,
            and_(
                Message.conversation_id == Conversation.id,
                Message.external_message_id == WebhookEvent.external_event_id,
            ),
        )
        .where(
            Channel.type == "max",
            Channel.status == "active",
            WebhookEvent.processed.is_(False),
            or_(
                WebhookEvent.processing_started_at.is_(None),
                WebhookEvent.processing_started_at < stale_before,
            ),
            Message.direction == "inbound",
            Message.sender_type == "customer",
        )
        .order_by(WebhookEvent.created_at)
        .limit(100)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True, of=WebhookEvent)
    event_ids = list((await session.execute(query)).scalars().all())
    processed = 0
    for event_id in event_ids:
        claim = await session.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.id == event_id,
                WebhookEvent.processed.is_(False),
                or_(
                    WebhookEvent.processing_started_at.is_(None),
                    WebhookEvent.processing_started_at < stale_before,
                ),
            )
            .values(processing_started_at=datetime.now(UTC))
            .returning(WebhookEvent.id)
        )
        if claim.scalar_one_or_none() is None:
            await session.rollback()
            continue
        await session.commit()
        event = await session.get(WebhookEvent, event_id)
        if event is None:
            continue
        inbound = (
            await session.execute(
                select(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Conversation.channel_id == event.channel_id,
                    Message.external_message_id == event.external_event_id,
                    Message.direction == "inbound",
                    Message.sender_type == "customer",
                )
            )
        ).scalar_one_or_none()
        if inbound is None:
            event.processed = True
            event.processing_started_at = None
            await session.commit()
            continue
        try:
            if not str((inbound.ai_meta or {}).get("decision") or ""):
                await process_channel_inbound_message(session, inbound.id)
            event = await session.get(WebhookEvent, event_id)
            if event is not None:
                event.processed = True
                event.processing_started_at = None
                await session.commit()
            processed += 1
        except Exception:
            await session.rollback()
            event = await session.get(WebhookEvent, event_id)
            if event is not None and not event.processed:
                event.processing_started_at = None
                await session.commit()
            raise
    return {"processed": processed}


async def _max_call(
    method: str,
    path: str,
    bot_token: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=settings.MAX_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.request(
                method,
                f"{settings.MAX_API_BASE_URL.rstrip('/')}{path}",
                headers={"Authorization": bot_token},
                params=params,
                json=json_body,
            )
        response.raise_for_status()
        if not response.content:
            return {}
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "MAX API request failed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid MAX API response")
    return payload


def _credentials(channel: Channel) -> dict[str, str]:
    try:
        payload = json.loads(decrypt_secret(channel.credentials_encrypted))
        return {
            "bot_token": str(payload["bot_token"]),
            "webhook_secret": str(payload["webhook_secret"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "MAX credentials are not configured",
        ) from exc


async def _owned_channel(session: AsyncSession, tenant_id: UUID, channel_id: UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id or channel.type != "max":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MAX channel not found")
    return channel


async def _get_or_create_customer(
    session: AsyncSession, channel: Channel, message: NormalizedMessage
) -> Customer:
    row = (
        await session.execute(
            select(Customer, CustomerIdentity)
            .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
            .where(
                CustomerIdentity.channel_id == channel.id,
                CustomerIdentity.external_user_id == message.customer_ref,
            )
        )
    ).first()
    if row:
        return row[0]
    customer = Customer(tenant_id=channel.tenant_id, display_name=message.customer_name or "MAX")
    try:
        async with session.begin_nested():
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
    except IntegrityError:
        concurrent = (
            await session.execute(
                select(Customer)
                .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
                .where(
                    CustomerIdentity.channel_id == channel.id,
                    CustomerIdentity.external_user_id == message.customer_ref,
                )
            )
        ).scalar_one_or_none()
        if concurrent is None:
            raise
        return concurrent


async def _get_or_create_conversation(
    session: AsyncSession,
    channel: Channel,
    customer: Customer,
    message: NormalizedMessage,
) -> Conversation:
    filters = (
        Conversation.tenant_id == channel.tenant_id,
        Conversation.channel_id == channel.id,
        Conversation.customer_id == customer.id,
        Conversation.external_conversation_id == message.external_conversation_id,
    )
    existing = (await session.execute(select(Conversation).where(*filters))).scalar_one_or_none()
    if existing is not None:
        if existing.status in {"closed", "snoozed"}:
            existing.status = "open"
            existing.assignee_user_id = None
        return existing
    conversation = Conversation(
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        customer_id=customer.id,
        external_conversation_id=message.external_conversation_id,
        status="open",
        last_message_preview=message.text,
    )
    try:
        async with session.begin_nested():
            session.add(conversation)
            await session.flush()
        return conversation
    except IntegrityError:
        concurrent = (
            await session.execute(select(Conversation).where(*filters))
        ).scalar_one_or_none()
        if concurrent is None:
            raise
        return concurrent


def _channel_response(channel: Channel) -> ChannelResponse:
    return ChannelResponse(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        status=channel.status,
        settings=dict(channel.settings or {}),
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )
