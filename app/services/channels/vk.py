"""VK Community Messages integration through the Callback API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
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
from app.schemas.channels import ChannelResponse, ChannelWebhookResponse, VkConnectRequest
from app.services.channels.base import DeliveryResult, NormalizedMessage

VK_MAX_MESSAGE_LENGTH = 9000


@dataclass(frozen=True)
class VkCredentials:
    access_token: str
    callback_confirmation: str
    callback_secret: str


async def connect_vk_channel(
    session: AsyncSession,
    tenant_id: UUID,
    body: VkConnectRequest,
) -> ChannelResponse:
    """Probe a community token before atomically activating the channel."""
    replacing: Channel | None = None
    if body.replace_channel_id is not None:
        replacing = await _owned_channel(session, tenant_id, body.replace_channel_id)

    group = await _probe_group(body.group_id, body.access_token)
    provider_group_id = str(group.get("id") or "")
    if provider_group_id != str(body.group_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "VK group mismatch")

    external_identity = f"vk:{body.group_id}"
    result = await session.execute(
        select(Channel).where(Channel.external_identity == external_identity)
    )
    matching = result.scalar_one_or_none()
    if matching is not None and matching.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This VK community is already connected")
    if replacing is not None and matching is not None and matching.id != replacing.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This VK community is already connected")
    if (
        replacing is not None
        and replacing.external_identity
        and replacing.external_identity != external_identity
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Replace can only rotate credentials for the same VK community",
        )

    channel = replacing or matching
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type="vk")
        session.add(channel)
    await session.flush()

    channel.type = "vk"
    channel.name = body.name.strip() or str(group.get("name") or "VK")
    channel.status = "active"
    channel.external_identity = external_identity
    channel.credentials_encrypted = encrypt_secret(
        json.dumps(
            {
                "access_token": body.access_token,
                "callback_confirmation": body.callback_confirmation,
                "callback_secret": body.callback_secret,
            },
            separators=(",", ":"),
        )
    )
    callback_path = f"/api/v1/channels/webhook/vk/{channel.id}"
    channel.settings = {
        "group_id": body.group_id,
        "group_name": str(group.get("name") or ""),
        "screen_name": str(group.get("screen_name") or ""),
        "callback_url": f"{settings.API_PUBLIC_URL.rstrip('/')}{callback_path}",
    }
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This VK community is already connected"
        ) from exc
    await session.refresh(channel)
    return _channel_response(channel)


async def process_vk_callback(
    session: AsyncSession,
    channel_id: UUID,
    payload: dict[str, Any],
) -> tuple[str, ChannelWebhookResponse | None]:
    """Validate and durably persist a callback before acknowledging it.

    VK retries non-200 responses. AI processing and provider delivery therefore
    happen later in the worker and never delay the exact ``ok`` acknowledgement.
    """
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.type != "vk" or channel.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active VK channel not found")
    credentials = _credentials(channel)
    expected_group_id = str((channel.settings or {}).get("group_id") or "")
    if str(payload.get("group_id") or "") != expected_group_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid VK callback group")
    if not secrets.compare_digest(str(payload.get("secret") or ""), credentials.callback_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid VK callback secret")

    if payload.get("type") == "confirmation":
        return credentials.callback_confirmation, None
    if payload.get("type") != "message_new":
        return "ok", ChannelWebhookResponse(ok=True, channel_id=channel.id)

    raw_object = payload.get("object")
    raw_message = raw_object.get("message") if isinstance(raw_object, dict) else None
    if not isinstance(raw_message, dict):
        return "ok", ChannelWebhookResponse(ok=True, channel_id=channel.id)

    peer_id = str(raw_message.get("peer_id") or "")
    from_id = str(raw_message.get("from_id") or "")
    text = str(raw_message.get("text") or "").strip()
    is_outgoing = bool(raw_message.get("out"))
    # Text-only, one-to-one MVP. Group chats, outgoing echoes, bots and
    # attachment-only events are acknowledged without entering the AI pipeline.
    # For a direct Community Messages dialog VK documents peer_id == from_id;
    # multi-user chats use a 2_000_000_000-based peer id and stay out of this MVP.
    if (
        is_outgoing
        or not peer_id
        or not from_id
        or not from_id.isdigit()
        or int(from_id) <= 0
        or peer_id != from_id
        or not text
    ):
        return "ok", ChannelWebhookResponse(ok=True, channel_id=channel.id)

    canonical_id = _canonical_message_id(raw_message, peer_id)
    if canonical_id is None:
        return "ok", ChannelWebhookResponse(ok=True, channel_id=channel.id)
    existing = await session.execute(
        select(WebhookEvent).where(
            WebhookEvent.channel_id == channel.id,
            WebhookEvent.external_event_id == canonical_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return "ok", ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)

    event = WebhookEvent(
        channel_id=channel.id,
        external_event_id=canonical_id,
        payload={
            "type": "message_new",
            "event_id": str(payload.get("event_id") or ""),
            "peer_id": peer_id,
            "provider_message_id": canonical_id,
        },
        processed=False,
    )
    session.add(event)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return "ok", ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)

    normalized = NormalizedMessage(
        channel="vk",
        external_conversation_id=peer_id,
        external_message_id=canonical_id,
        customer_ref=from_id,
        customer_name=f"VK {from_id}",
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
        attachments=_compact_attachment_metadata(raw_message),
        external_message_id=canonical_id,
        status="received",
        ai_meta={
            "source": "vk",
            "chat_id": peer_id,
            "webhook_event_id": canonical_id,
            "vk_event_id": str(payload.get("event_id") or ""),
        },
    )
    session.add(inbound)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return "ok", ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)
    await session.commit()
    return "ok", ChannelWebhookResponse(
        ok=True,
        channel_id=channel.id,
        conversation_id=conversation.id,
        inbound_message_id=inbound.id,
        processed_count=1,
    )


async def send_vk_message(
    channel: Channel,
    peer_id: str,
    text: str,
    *,
    idempotency_key: str,
) -> DeliveryResult:
    """Send text with a retry-stable VK ``random_id``."""
    if len(text) > VK_MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"VK message exceeds {VK_MAX_MESSAGE_LENGTH} characters",
        )
    credentials = _credentials(channel)
    payload = await _vk_call(
        "messages.send",
        credentials.access_token,
        {
            "peer_id": peer_id,
            "random_id": _stable_random_id(idempotency_key),
            "message": text,
        },
        retry_rate_limit=True,
    )
    raw_message_id = payload.get("message_id") if isinstance(payload, dict) else payload
    message_id = str(raw_message_id or "")
    return DeliveryResult(
        delivered=bool(message_id),
        external_message_id=message_id or None,
        status="sent" if message_id else "failed",
        metadata={"delivery": "vk-community"},
    )


async def process_pending_vk(session: AsyncSession) -> dict[str, int]:
    """Claim and process durable VK callbacks exactly once across cron workers.

    PostgreSQL uses ``SKIP LOCKED`` while the conditional UPDATE remains the
    authoritative compare-and-swap claim. The latter also makes local SQLite
    workers safe when two cron invocations select the same candidate.
    """
    from app.services.channels.telegram import process_channel_inbound_message

    stale_before = datetime.now(UTC) - timedelta(minutes=5)
    candidate_query = (
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
            Channel.type == "vk",
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
        candidate_query = candidate_query.with_for_update(skip_locked=True, of=WebhookEvent)
    candidate_ids = list((await session.execute(candidate_query)).scalars().all())

    processed = 0
    for event_id in candidate_ids:
        claimed_at = datetime.now(UTC)
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
            .values(processing_started_at=claimed_at)
            .returning(WebhookEvent.id)
        )
        if claim.scalar_one_or_none() is None:
            await session.rollback()
            continue
        await session.commit()

        event = await session.get(WebhookEvent, event_id)
        if event is None:
            continue
        inbound_result = await session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.channel_id == event.channel_id,
                Message.external_message_id == event.external_event_id,
                Message.direction == "inbound",
                Message.sender_type == "customer",
            )
        )
        inbound = inbound_result.scalar_one_or_none()
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


async def _probe_group(group_id: int, access_token: str) -> dict[str, Any]:
    payload = await _vk_call(
        "groups.getById",
        access_token,
        {"group_id": group_id, "fields": "name,screen_name"},
    )
    groups = payload.get("groups") if isinstance(payload, dict) else payload
    first = groups[0] if isinstance(groups, list) and groups else None
    if not isinstance(first, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid VK group response")
    return first


async def _vk_call(
    method: str,
    access_token: str,
    params: dict[str, Any],
    *,
    retry_rate_limit: bool = False,
) -> Any:
    attempts = 3 if retry_rate_limit else 1
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=settings.VK_DELIVERY_TIMEOUT_SEC) as client:
                response = await client.post(
                    f"{settings.VK_API_BASE_URL.rstrip('/')}/{method}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    data={**params, "v": settings.VK_API_VERSION},
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.05 * (attempt + 1))
                    continue
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "VK API request failed") from exc
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            error_code = int(error.get("error_code") or 0)
            if error_code == 6 and attempt + 1 < attempts:
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"VK API request failed ({error_code or 'unknown'})",
            )
        if not isinstance(payload, dict) or "response" not in payload:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "VK API request failed")
        return payload["response"]
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "VK API request failed")


def _credentials(channel: Channel) -> VkCredentials:
    try:
        payload = json.loads(decrypt_secret(channel.credentials_encrypted))
        return VkCredentials(
            access_token=str(payload["access_token"]),
            callback_confirmation=str(payload["callback_confirmation"]),
            callback_secret=str(payload["callback_secret"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "VK credentials are not configured"
        ) from exc


async def _owned_channel(session: AsyncSession, tenant_id: UUID, channel_id: UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id or channel.type != "vk":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "VK channel not found")
    return channel


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
        return row[0]
    customer = Customer(tenant_id=channel.tenant_id, display_name=message.customer_name or "VK")
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
        concurrent = await session.execute(
            select(Customer)
            .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
            .where(
                CustomerIdentity.channel_id == channel.id,
                CustomerIdentity.external_user_id == message.customer_ref,
            )
        )
        existing = concurrent.scalar_one_or_none()
        if existing is None:
            raise
        return existing


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
    result = await session.execute(select(Conversation).where(*filters))
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        if conversation.status in {"closed", "snoozed"}:
            conversation.status = "open"
            conversation.assignee_user_id = None
        return conversation
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
        existing = (
            await session.execute(select(Conversation).where(*filters))
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing


def _canonical_message_id(raw_message: dict[str, Any], peer_id: str) -> str | None:
    provider_id = str(raw_message.get("id") or "")
    if provider_id and provider_id != "0":
        return f"message:{provider_id}"
    conversation_id = str(raw_message.get("conversation_message_id") or "")
    if conversation_id:
        return f"cmid:{peer_id}:{conversation_id}"
    return None


def _compact_attachment_metadata(raw_message: dict[str, Any]) -> dict[str, Any]:
    attachments = raw_message.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return {}
    return {
        "provider": "vk",
        "count": len(attachments),
        "types": [
            str(item.get("type") or "unknown") for item in attachments if isinstance(item, dict)
        ],
    }


def _stable_random_id(idempotency_key: str) -> int:
    value = int.from_bytes(hashlib.sha256(idempotency_key.encode()).digest()[:4], "big")
    return (value & 0x7FFFFFFF) or 1


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
