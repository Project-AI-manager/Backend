"""WhatsApp Business Cloud API channel integration."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
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
from app.schemas.channels import (
    ChannelProbeResponse,
    ChannelResponse,
    ChannelWebhookResponse,
    WhatsAppConnectRequest,
)
from app.services.channels.base import DeliveryResult, NormalizedMessage

GRAPH_API_VERSION = "v23.0"


@dataclass(frozen=True)
class WhatsAppCredentials:
    access_token: str
    app_secret: str
    verify_token: str


def _credentials(channel: Channel) -> WhatsAppCredentials:
    try:
        raw = json.loads(decrypt_secret(channel.credentials_encrypted))
        return WhatsAppCredentials(
            access_token=str(raw["access_token"]),
            app_secret=str(raw["app_secret"]),
            verify_token=str(raw["verify_token"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "WhatsApp credentials are not configured"
        ) from exc


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


async def connect_whatsapp_channel(
    session: AsyncSession,
    tenant_id: UUID,
    body: WhatsAppConnectRequest,
) -> ChannelResponse:
    replacing: Channel | None = None
    if body.replace_channel_id is not None:
        replacing = await _owned_channel(session, tenant_id, body.replace_channel_id)

    external_identity = f"whatsapp:{body.phone_number_id}"
    result = await session.execute(
        select(Channel).where(Channel.external_identity == external_identity)
    )
    matching = result.scalar_one_or_none()
    if matching is not None and matching.tenant_id != tenant_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This WhatsApp phone number is already connected"
        )
    if replacing is not None and matching is not None and matching.id != replacing.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This WhatsApp phone number is already connected"
        )
    if (
        replacing is not None
        and replacing.external_identity
        and replacing.external_identity != external_identity
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Replace can only rotate credentials for the same WhatsApp phone number",
        )
    channel = replacing or matching
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type="whatsapp")
        session.add(channel)
    credentials_encrypted = encrypt_secret(
        json.dumps(
            {
                "access_token": body.access_token,
                "app_secret": body.app_secret,
                "verify_token": body.verify_token,
            },
            separators=(",", ":"),
        )
    )
    credentials = WhatsAppCredentials(
        access_token=body.access_token,
        app_secret=body.app_secret,
        verify_token=body.verify_token,
    )
    probe = await _probe_credentials(body.waba_id, body.phone_number_id, credentials)
    channel.name = body.name.strip() or "WhatsApp"
    channel.status = "active"
    channel.external_identity = external_identity
    channel.credentials_encrypted = credentials_encrypted
    channel.settings = {
        "phone_number_id": body.phone_number_id,
        "waba_id": body.waba_id,
        "webhook_path": f"/api/v1/channels/webhook/whatsapp/{body.phone_number_id}",
        "display_phone_number": probe.display_phone_number,
        "verified_name": probe.verified_name,
    }
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This WhatsApp phone number is already connected"
        ) from exc
    await session.refresh(channel)
    return _channel_response(channel)


async def probe_whatsapp_channel(
    session: AsyncSession, tenant_id: UUID, channel_id: UUID
) -> ChannelProbeResponse:
    channel = await _owned_channel(session, tenant_id, channel_id)
    phone_number_id = str((channel.settings or {}).get("phone_number_id") or "")
    waba_id = str((channel.settings or {}).get("waba_id") or "")
    return await _probe_credentials(waba_id, phone_number_id, _credentials(channel))


async def _probe_credentials(
    waba_id: str, phone_number_id: str, credentials: WhatsAppCredentials
) -> ChannelProbeResponse:
    try:
        async with httpx.AsyncClient(timeout=settings.WHATSAPP_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{settings.WHATSAPP_GRAPH_BASE_URL}/{GRAPH_API_VERSION}/{waba_id}/phone_numbers",
                params={"fields": "id,display_phone_number,verified_name"},
                headers={"Authorization": f"Bearer {credentials.access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "WhatsApp probe failed") from exc
    numbers = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(numbers, list):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid Meta response")
    payload = next(
        (
            item
            for item in numbers
            if isinstance(item, dict) and str(item.get("id") or "") == phone_number_id
        ),
        {},
    )
    if not payload:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "WhatsApp number mismatch")
    return ChannelProbeResponse(
        ok=True,
        display_phone_number=str(payload.get("display_phone_number") or ""),
        verified_name=str(payload.get("verified_name") or ""),
    )


async def verify_whatsapp_webhook(
    session: AsyncSession,
    phone_number_id: str,
    mode: str,
    verify_token: str,
    challenge: str,
) -> str:
    channel = await _channel_by_phone_number_id(session, phone_number_id)
    expected = _credentials(channel).verify_token
    if mode != "subscribe" or not hmac.compare_digest(expected, verify_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "WhatsApp webhook verification failed")
    return challenge


async def process_whatsapp_webhook(
    session: AsyncSession,
    phone_number_id: str,
    raw_body: bytes,
    signature: str | None,
) -> ChannelWebhookResponse:
    channel = await _channel_by_phone_number_id(session, phone_number_id)
    _verify_signature(_credentials(channel).app_secret, raw_body, signature)
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid WhatsApp webhook JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid WhatsApp webhook payload")

    response = ChannelWebhookResponse(ok=True, channel_id=channel.id)
    for entry_id, value in _values(payload):
        if not _value_belongs_to_channel(channel, phone_number_id, entry_id, value):
            continue
        await _apply_statuses(session, channel, value)
        contact_names = _contact_names(value)
        for raw_message in value.get("messages") or []:
            if not isinstance(raw_message, dict) or raw_message.get("type") != "text":
                continue
            event_id = str(raw_message.get("id") or "")
            sender = str(raw_message.get("from") or "")
            text_value = raw_message.get("text") or {}
            text = str(text_value.get("body") or "").strip() if isinstance(text_value, dict) else ""
            if not event_id or not sender or not text:
                continue
            duplicate, result = await _ingest_message(
                session,
                channel,
                event_id,
                NormalizedMessage(
                    channel="whatsapp",
                    external_conversation_id=sender,
                    external_message_id=event_id,
                    customer_ref=sender,
                    customer_name=contact_names.get(sender, sender),
                    text=text,
                ),
            )
            if duplicate:
                response.duplicate = True
                continue
            response.processed_count += 1
            response.conversation_id = result.conversation_id
            response.inbound_message_id = result.inbound_message_id
            response.outbound_message_id = result.outbound_message_id
            response.decision = result.decision
    await session.commit()
    return response


async def send_whatsapp_message(channel: Channel, recipient: str, text: str) -> DeliveryResult:
    credentials = _credentials(channel)
    phone_number_id = str((channel.settings or {}).get("phone_number_id") or "")
    try:
        async with httpx.AsyncClient(timeout=settings.WHATSAPP_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{settings.WHATSAPP_GRAPH_BASE_URL}/{GRAPH_API_VERSION}/{phone_number_id}/messages",
                headers={"Authorization": f"Bearer {credentials.access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient,
                    "type": "text",
                    "text": {"preview_url": False, "body": text},
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "WhatsApp delivery failed") from exc
    messages = payload.get("messages") or []
    wamid = str(messages[0].get("id") or "") if messages and isinstance(messages[0], dict) else ""
    return DeliveryResult(
        delivered=True,
        external_message_id=wamid or None,
        status="sent",
        metadata={"delivery": "whatsapp-cloud-api"},
    )


async def _ingest_message(
    session: AsyncSession,
    channel: Channel,
    event_id: str,
    normalized: NormalizedMessage,
) -> tuple[bool, ChannelWebhookResponse]:
    existing_event = await session.execute(
        select(WebhookEvent).where(
            WebhookEvent.channel_id == channel.id,
            WebhookEvent.external_event_id == event_id,
        )
    )
    if existing_event.scalar_one_or_none() is not None:
        return True, ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)
    event = WebhookEvent(
        channel_id=channel.id,
        external_event_id=event_id,
        payload={
            "type": "message",
            "message_id": event_id,
            "from": normalized.customer_ref,
        },
        processed=False,
    )
    session.add(event)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return True, ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)
    customer = await _get_or_create_customer(session, channel, normalized)
    conversation = await _get_or_create_conversation(session, channel, customer, normalized)
    inbound = Message(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        sender_type="customer",
        sender_user_id=None,
        text=normalized.text,
        attachments={},
        external_message_id=event_id,
        status="received",
        confidence=None,
        ai_meta={"source": "whatsapp", "chat_id": normalized.external_conversation_id},
    )
    session.add(inbound)
    await session.flush()
    from app.services.channels.telegram import process_channel_inbound_message

    result = await process_channel_inbound_message(session, inbound.id)
    event.processed = True
    return False, result


async def _get_or_create_customer(
    session: AsyncSession, channel: Channel, message: NormalizedMessage
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
        customer, _identity = row
        customer.display_name = message.customer_name or customer.display_name
        return customer
    customer = Customer(
        tenant_id=channel.tenant_id, display_name=message.customer_name or "WhatsApp customer"
    )
    identity = CustomerIdentity(
            customer_id=customer.id,
            channel_id=channel.id,
            external_user_id=message.customer_ref,
        )
    try:
        async with session.begin_nested():
            session.add(customer)
            await session.flush()
            identity.customer_id = customer.id
            session.add(identity)
            await session.flush()
        return customer
    except IntegrityError:
        concurrent = await session.execute(
            select(Customer, CustomerIdentity)
            .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
            .where(
                CustomerIdentity.channel_id == channel.id,
                CustomerIdentity.external_user_id == message.customer_ref,
            )
        )
        row = concurrent.first()
        if row is None:
            raise
        existing, _identity = row
        existing.display_name = message.customer_name or existing.display_name
        return existing


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
            Conversation.external_conversation_id == message.external_conversation_id,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        if conversation.status in {"closed", "snoozed"}:
            conversation.status = "open"
            conversation.assignee_user_id = None
        return conversation
    conversation = Conversation(
        tenant_id=channel.tenant_id,
        customer_id=customer.id,
        channel_id=channel.id,
        external_conversation_id=message.external_conversation_id,
        status="open",
        last_message_preview=message.text,
        unread_count=0,
    )
    try:
        async with session.begin_nested():
            session.add(conversation)
            await session.flush()
        return conversation
    except IntegrityError:
        concurrent = await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == channel.tenant_id,
                Conversation.channel_id == channel.id,
                Conversation.customer_id == customer.id,
                Conversation.external_conversation_id == message.external_conversation_id,
            )
        )
        existing = concurrent.scalar_one_or_none()
        if existing is None:
            raise
        if existing.status in {"closed", "snoozed"}:
            existing.status = "open"
            existing.assignee_user_id = None
        return existing


async def _apply_statuses(session: AsyncSession, channel: Channel, value: dict[str, Any]) -> None:
    for item in value.get("statuses") or []:
        if not isinstance(item, dict):
            continue
        external_id = str(item.get("id") or "")
        new_status = str(item.get("status") or "")
        if not external_id or new_status not in {"sent", "delivered", "read", "failed"}:
            continue
        status_event_id = f"status:{external_id}:{new_status}"
        existing_event = await session.execute(
            select(WebhookEvent).where(
                WebhookEvent.channel_id == channel.id,
                WebhookEvent.external_event_id == status_event_id,
            )
        )
        if existing_event.scalar_one_or_none() is not None:
            continue
        try:
            async with session.begin_nested():
                session.add(
                    WebhookEvent(
                        channel_id=channel.id,
                        external_event_id=status_event_id,
                        payload=item,
                        processed=True,
                    )
                )
                await session.flush()
        except IntegrityError:
            continue
        result = await session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.channel_id == channel.id,
                Message.external_message_id == external_id,
            )
        )
        message = result.scalar_one_or_none()
        if message is not None:
            rank = {"pending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
            current_rank = rank.get(message.status, 0)
            next_rank = rank[new_status]
            if new_status == "failed" or next_rank >= current_rank:
                message.status = new_status
            message.ai_meta = {**(message.ai_meta or {}), "whatsapp_status": item}


def _values(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    values: list[tuple[str, dict[str, Any]]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if isinstance(value, dict):
                values.append((entry_id, value))
    return values


def _value_belongs_to_channel(
    channel: Channel,
    phone_number_id: str,
    entry_id: str,
    value: dict[str, Any],
) -> bool:
    metadata = value.get("metadata")
    return bool(
        value.get("messaging_product") == "whatsapp"
        and entry_id == str((channel.settings or {}).get("waba_id") or "")
        and isinstance(metadata, dict)
        and str(metadata.get("phone_number_id") or "") == phone_number_id
        and phone_number_id == str((channel.settings or {}).get("phone_number_id") or "")
    )


def _contact_names(value: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        wa_id = str(contact.get("wa_id") or "")
        profile = contact.get("profile") or {}
        if wa_id and isinstance(profile, dict):
            names[wa_id] = str(profile.get("name") or wa_id)
    return names


def _verify_signature(app_secret: str, raw_body: bytes, signature: str | None) -> None:
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid WhatsApp webhook signature")


async def _channel_by_phone_number_id(session: AsyncSession, phone_number_id: str) -> Channel:
    result = await session.execute(
        select(Channel).where(
            Channel.type == "whatsapp",
            Channel.status == "active",
            Channel.external_identity == f"whatsapp:{phone_number_id}",
        )
    )
    channel = result.scalar_one_or_none()
    if channel is not None:
        return channel
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Active WhatsApp channel not found")


async def _owned_channel(session: AsyncSession, tenant_id: UUID, channel_id: UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id or channel.type != "whatsapp":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "WhatsApp channel not found")
    return channel
