"""Instagram Messaging API integration using Instagram Login."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.models.channel import Channel, InstagramOAuthAttempt, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.schemas.channels import (
    ChannelResponse,
    ChannelWebhookResponse,
    InstagramOAuthStartResponse,
)
from app.services.channels.base import DeliveryResult, NormalizedMessage

INSTAGRAM_OAUTH_COOKIE = "instagram_oauth_binding"
INSTAGRAM_OAUTH_TTL = timedelta(minutes=10)
INSTAGRAM_PROCESSING_LEASE = timedelta(minutes=5)
INSTAGRAM_MAX_MESSAGE_LENGTH = 1000


@dataclass(frozen=True)
class InstagramCredentials:
    access_token: str


def instagram_callback_url(api_public_url: str) -> str:
    return f"{api_public_url.rstrip('/')}/api/v1/channels/instagram/oauth/callback"


async def start_instagram_oauth(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    api_public_url: str,
    *,
    replace_channel_id: UUID | None = None,
) -> tuple[InstagramOAuthStartResponse, str]:
    if not settings.INSTAGRAM_APP_ID or not settings.INSTAGRAM_APP_SECRET:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Instagram OAuth is not configured"
        )
    if replace_channel_id is not None:
        await _owned_channel(session, tenant_id, replace_channel_id)

    state_token = secrets.token_urlsafe(32)
    browser_binding = secrets.token_urlsafe(32)
    session.add(
        InstagramOAuthAttempt(
            state_hash=_secret_hash(state_token),
            browser_binding_hash=_secret_hash(browser_binding),
            tenant_id=tenant_id,
            user_id=user_id,
            replace_channel_id=replace_channel_id,
            expires_at=datetime.now(UTC) + INSTAGRAM_OAUTH_TTL,
        )
    )
    await session.commit()
    query = urlencode(
        {
            "client_id": settings.INSTAGRAM_APP_ID,
            "redirect_uri": instagram_callback_url(api_public_url),
            "response_type": "code",
            "scope": "instagram_business_basic,instagram_business_manage_messages",
            "state": state_token,
        }
    )
    return (
        InstagramOAuthStartResponse(
            authorization_url=f"{settings.INSTAGRAM_OAUTH_AUTHORIZE_URL}?{query}"
        ),
        browser_binding,
    )


async def consume_instagram_oauth_attempt(
    session: AsyncSession,
    state_token: str,
    browser_binding: str,
) -> tuple[UUID, UUID, UUID | None]:
    if not state_token or not browser_binding:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Instagram OAuth state")
    now = datetime.now(UTC)
    result = await session.execute(
        update(InstagramOAuthAttempt)
        .where(
            InstagramOAuthAttempt.state_hash == _secret_hash(state_token),
            InstagramOAuthAttempt.browser_binding_hash == _secret_hash(browser_binding),
            InstagramOAuthAttempt.consumed_at.is_(None),
            InstagramOAuthAttempt.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(
            InstagramOAuthAttempt.tenant_id,
            InstagramOAuthAttempt.user_id,
            InstagramOAuthAttempt.replace_channel_id,
        )
    )
    identity = result.first()
    await session.commit()
    if identity is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Instagram OAuth state")
    return identity[0], identity[1], identity[2]


async def complete_instagram_oauth(
    session: AsyncSession,
    code: str,
    state_token: str,
    browser_binding: str,
    api_public_url: str,
) -> ChannelResponse:
    tenant_id, _user_id, replace_channel_id = await consume_instagram_oauth_attempt(
        session, state_token, browser_binding
    )
    short_token = await _exchange_code(code, api_public_url)
    access_token = await _exchange_long_lived_token(short_token)
    profile = await _graph_call(
        "GET",
        "/me",
        access_token,
        params={"fields": "user_id,username,name"},
    )
    instagram_user_id = str(profile.get("user_id") or profile.get("id") or "")
    if not instagram_user_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Instagram profile response")

    replacing = (
        await _owned_channel(session, tenant_id, replace_channel_id)
        if replace_channel_id is not None
        else None
    )
    external_identity = f"instagram:{instagram_user_id}"
    matching = (
        await session.execute(select(Channel).where(Channel.external_identity == external_identity))
    ).scalar_one_or_none()
    if matching is not None and matching.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This Instagram account is already connected")
    if replacing is not None and matching is not None and matching.id != replacing.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This Instagram account is already connected")
    if (
        replacing is not None
        and replacing.external_identity
        and replacing.external_identity != external_identity
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Replace can only rotate credentials for the same Instagram account",
        )

    channel = replacing or matching
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type="instagram")
        session.add(channel)
    channel.type = "instagram"
    channel.name = str(profile.get("name") or profile.get("username") or "Instagram")[:255]
    channel.status = "active"
    channel.external_identity = external_identity
    channel.credentials_encrypted = encrypt_secret(
        json.dumps({"access_token": access_token}, separators=(",", ":"))
    )
    channel.webhook_identity = secrets.token_hex(32)
    channel.settings = {
        "instagram_user_id": instagram_user_id,
        "username": str(profile.get("username") or ""),
        "display_name": str(profile.get("name") or ""),
    }
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This Instagram account is already connected"
        ) from exc
    await session.refresh(channel)

    try:
        await _graph_call(
            "POST",
            f"/{instagram_user_id}/subscribed_apps",
            access_token,
            params={"subscribed_fields": "messages"},
        )
    except HTTPException:
        channel.status = "error"
        await session.commit()
        raise
    return _channel_response(channel)


async def verify_instagram_webhook(mode: str, verify_token: str, challenge: str) -> str:
    configured = settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN
    if (
        mode != "subscribe"
        or not configured
        or not secrets.compare_digest(verify_token, configured)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid Instagram verify token")
    return challenge


async def process_instagram_webhook(
    session: AsyncSession,
    raw_body: bytes,
    signature: str | None,
) -> ChannelWebhookResponse:
    _verify_signature(raw_body, signature)
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Instagram webhook JSON") from exc
    if not isinstance(payload, dict) or payload.get("object") != "instagram":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid Instagram webhook payload")

    processed_count = 0
    duplicate = False
    last_result = ChannelWebhookResponse(ok=True)
    entries = payload.get("entry")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        instagram_user_id = str(entry.get("id") or "")
        messaging = entry.get("messaging")
        if not instagram_user_id or not isinstance(messaging, list) or not messaging:
            continue
        channel = await _active_channel(session, instagram_user_id)
        for event in messaging:
            result = await _persist_event(session, channel, event)
            if result.duplicate:
                duplicate = True
            if result.inbound_message_id is not None:
                processed_count += 1
                last_result = result
    last_result.duplicate = duplicate and processed_count == 0
    last_result.processed_count = processed_count
    return last_result


async def _persist_event(
    session: AsyncSession,
    channel: Channel,
    raw_event: object,
) -> ChannelWebhookResponse:
    if not isinstance(raw_event, dict):
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    message = raw_event.get("message")
    sender = raw_event.get("sender")
    recipient = raw_event.get("recipient")
    if (
        not isinstance(message, dict)
        or not isinstance(sender, dict)
        or not isinstance(recipient, dict)
    ):
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    sender_id = str(sender.get("id") or "")
    recipient_id = str(recipient.get("id") or "")
    expected_recipient = str((channel.settings or {}).get("instagram_user_id") or "")
    message_id = str(message.get("mid") or "")
    text = str(message.get("text") or "").strip()
    if (
        not sender_id
        or not message_id
        or not text
        or recipient_id != expected_recipient
        or bool(message.get("is_echo"))
    ):
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
        payload={"type": "message", "message_id": message_id, "sender_id": sender_id},
        processed=False,
    )
    session.add(event)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)

    normalized = NormalizedMessage(
        channel="instagram",
        external_conversation_id=sender_id,
        external_message_id=message_id,
        customer_ref=sender_id,
        customer_name=f"Instagram {sender_id}",
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
        ai_meta={
            "source": "instagram",
            "chat_id": sender_id,
            "webhook_event_id": message_id,
        },
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
    )


async def send_instagram_message(
    channel: Channel,
    recipient_id: str,
    text: str,
) -> DeliveryResult:
    if len(text.encode("utf-8")) > INSTAGRAM_MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Instagram message exceeds {INSTAGRAM_MAX_MESSAGE_LENGTH} UTF-8 bytes",
        )
    payload = await _graph_call(
        "POST",
        f"/{(channel.settings or {}).get('instagram_user_id')}/messages",
        _credentials(channel).access_token,
        json_body={"recipient": {"id": recipient_id}, "message": {"text": text}},
    )
    message_id = str(payload.get("message_id") or "")
    return DeliveryResult(
        delivered=bool(message_id),
        external_message_id=message_id or None,
        status="sent" if message_id else "failed",
        metadata={"delivery": "instagram-messaging"},
    )


async def unsubscribe_instagram_webhook(channel: Channel) -> None:
    instagram_user_id = str((channel.settings or {}).get("instagram_user_id") or "")
    if instagram_user_id and channel.credentials_encrypted:
        await _graph_call(
            "DELETE",
            f"/{instagram_user_id}/subscribed_apps",
            _credentials(channel).access_token,
        )


async def process_pending_instagram(session: AsyncSession) -> dict[str, int]:
    return await _process_pending_events(session, "instagram")


async def _process_pending_events(session: AsyncSession, channel_type: str) -> dict[str, int]:
    from app.services.channels.telegram import process_channel_inbound_message

    stale_before = datetime.now(UTC) - INSTAGRAM_PROCESSING_LEASE
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
            Channel.type == channel_type,
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


async def _exchange_code(code: str, api_public_url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=settings.INSTAGRAM_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.post(
                settings.INSTAGRAM_OAUTH_TOKEN_URL,
                data={
                    "client_id": settings.INSTAGRAM_APP_ID,
                    "client_secret": settings.INSTAGRAM_APP_SECRET,
                    "grant_type": "authorization_code",
                    "redirect_uri": instagram_callback_url(api_public_url),
                    "code": code,
                },
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Instagram OAuth failed") from exc
    token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Instagram OAuth response")
    return token


async def _exchange_long_lived_token(short_token: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=settings.INSTAGRAM_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.get(
                f"{settings.INSTAGRAM_GRAPH_BASE_URL.rstrip('/')}/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": settings.INSTAGRAM_APP_SECRET,
                    "access_token": short_token,
                },
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Instagram OAuth failed") from exc
    token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Instagram OAuth response")
    return token


async def _graph_call(
    method: str,
    path: str,
    access_token: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = (
        f"{settings.INSTAGRAM_GRAPH_BASE_URL.rstrip('/')}/"
        f"{settings.INSTAGRAM_GRAPH_VERSION.strip('/')}{path}"
    )
    try:
        async with httpx.AsyncClient(timeout=settings.INSTAGRAM_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                json=json_body,
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Instagram API request failed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Instagram API response")
    return payload


def _verify_signature(raw_body: bytes, signature: str | None) -> None:
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Instagram signature")
    expected = hmac.new(
        settings.INSTAGRAM_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    if not secrets.compare_digest(signature.removeprefix("sha256="), expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Instagram signature")


def _credentials(channel: Channel) -> InstagramCredentials:
    try:
        payload = json.loads(decrypt_secret(channel.credentials_encrypted))
        return InstagramCredentials(access_token=str(payload["access_token"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Instagram credentials are not configured"
        ) from exc


async def _owned_channel(session: AsyncSession, tenant_id: UUID, channel_id: UUID) -> Channel:
    channel = await session.get(Channel, channel_id)
    if channel is None or channel.tenant_id != tenant_id or channel.type != "instagram":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instagram channel not found")
    return channel


async def _active_channel(session: AsyncSession, instagram_user_id: str) -> Channel:
    channel = (
        await session.execute(
            select(Channel).where(
                Channel.external_identity == f"instagram:{instagram_user_id}",
                Channel.type == "instagram",
                Channel.status == "active",
            )
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active Instagram channel not found")
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
    customer = Customer(
        tenant_id=channel.tenant_id, display_name=message.customer_name or "Instagram"
    )
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
        existing = (
            await session.execute(
                select(Customer)
                .join(CustomerIdentity, CustomerIdentity.customer_id == Customer.id)
                .where(
                    CustomerIdentity.channel_id == channel.id,
                    CustomerIdentity.external_user_id == message.customer_ref,
                )
            )
        ).scalar_one_or_none()
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


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
