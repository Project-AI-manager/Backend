"""Avito Messenger OAuth, webhook and text delivery integration."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.session import SessionLocal
from app.models.channel import AvitoOAuthAttempt, Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.schemas.channels import (
    AvitoOAuthStartResponse,
    ChannelResponse,
    ChannelWebhookResponse,
)
from app.services.channels.base import DeliveryResult, NormalizedMessage

AVITO_OAUTH_COOKIE = "avito_oauth_binding"
AVITO_OAUTH_TTL = timedelta(minutes=10)
AVITO_PROCESSING_LEASE = timedelta(minutes=5)


def _oauth_redirect_uri(api_public_url: str) -> str:
    return f"{api_public_url.rstrip('/')}/api/v1/channels/avito/oauth/callback"


async def start_avito_oauth(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, api_public_url: str
) -> tuple[AvitoOAuthStartResponse, str]:
    if not settings.AVITO_CLIENT_ID or not settings.AVITO_CLIENT_SECRET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Avito OAuth is not configured")
    state = secrets.token_urlsafe(32)
    browser_binding = secrets.token_urlsafe(32)
    session.add(
        AvitoOAuthAttempt(
            state_hash=_secret_hash(state),
            browser_binding_hash=_secret_hash(browser_binding),
            tenant_id=tenant_id,
            user_id=user_id,
            expires_at=datetime.now(UTC) + AVITO_OAUTH_TTL,
        )
    )
    await session.commit()
    query = urlencode(
        {
            "response_type": "code",
            "pro_users_flow": "true",
            "client_id": settings.AVITO_CLIENT_ID,
            "scope": "messenger:read messenger:write",
            "redirect_uri": _oauth_redirect_uri(api_public_url),
            "state": state,
        }
    )
    return (
        AvitoOAuthStartResponse(
            authorization_url=f"{settings.AVITO_OAUTH_AUTHORIZE_URL}?{query}"
        ),
        browser_binding,
    )


async def complete_avito_oauth(
    session: AsyncSession,
    code: str,
    state_token: str,
    browser_binding: str,
    api_public_url: str,
) -> ChannelResponse:
    tenant_id, _user_id = await _consume_oauth_attempt(
        session, state_token, browser_binding
    )
    token = await _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": settings.AVITO_CLIENT_ID,
            "client_secret": settings.AVITO_CLIENT_SECRET,
            "code": code,
            "redirect_uri": _oauth_redirect_uri(api_public_url),
        }
    )
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Avito token response")
    account = await _api_request("GET", "/core/v1/accounts/self", access_token=access_token)
    avito_user_id = str(account.get("id") or account.get("user_id") or "")
    if not avito_user_id:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Avito account response")

    external_identity = f"avito:{avito_user_id}"
    result = await session.execute(
        select(Channel).where(Channel.external_identity == external_identity)
    )
    channel = result.scalar_one_or_none()
    if channel is not None and channel.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "This Avito account is already connected")
    if channel is None:
        channel = Channel(tenant_id=tenant_id, type="avito", external_identity=external_identity)
        session.add(channel)

    webhook_secret = secrets.token_urlsafe(32)
    webhook_path = f"/api/v1/channels/webhook/avito/{webhook_secret}"
    webhook_url = f"{api_public_url.rstrip('/')}{webhook_path}"
    await _api_request(
        "POST",
        "/messenger/v3/webhook",
        access_token=access_token,
        json_body={"url": webhook_url},
    )
    expires_in = max(int(token.get("expires_in") or 86400), 60)
    credentials = {
        "access_token": access_token,
        "refresh_token": str(token.get("refresh_token") or ""),
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
    }
    channel.type = "avito"
    channel.name = str(account.get("name") or "Avito")[:255]
    channel.status = "active"
    channel.external_identity = external_identity
    channel.credentials_encrypted = encrypt_secret(json.dumps(credentials, separators=(",", ":")))
    channel.settings = {
        "user_id": avito_user_id,
        "account_name": str(account.get("name") or ""),
        "webhook_path": webhook_path,
    }
    channel.webhook_identity = _secret_hash(webhook_secret)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This Avito account is already connected"
        ) from exc
    await session.refresh(channel)
    return _channel_response(channel)


async def process_avito_webhook(
    session: AsyncSession, webhook_secret: str, payload: dict[str, Any]
) -> ChannelWebhookResponse:
    channel = await _channel_by_webhook_secret(session, webhook_secret)
    if not payload:
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)
    event_id = str(payload.get("id") or "")
    body = payload.get("payload")
    value = body.get("value") if isinstance(body, dict) else None
    if (
        not event_id
        or not isinstance(body, dict)
        or body.get("type") != "message"
        or not isinstance(value, dict)
        or str(value.get("user_id") or "") != str((channel.settings or {}).get("user_id") or "")
    ):
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)

    message_id = str(value.get("id") or "")
    chat_id = str(value.get("chat_id") or "")
    author_id = str(value.get("author_id") or "")
    message_type = str(value.get("type") or "")
    content = value.get("content")
    text_value = content.get("text") if isinstance(content, dict) else ""
    text = str(text_value or "").strip()
    own_user_id = str((channel.settings or {}).get("user_id") or "")
    if not message_id or not chat_id or not author_id or author_id == own_user_id:
        return ChannelWebhookResponse(ok=True, channel_id=channel.id)

    duplicate = await _event_exists(session, channel.id, event_id, message_id)
    if duplicate:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)
    event = WebhookEvent(
        channel_id=channel.id,
        external_event_id=message_id,
        payload={"type": message_type, "message_id": message_id, "chat_id": chat_id},
        processed=False,
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)

    if message_type != "text" or not text:
        event.processed = True
        await session.commit()
        return ChannelWebhookResponse(ok=True, channel_id=channel.id, processed_count=0)

    customer_name = str(value.get("author_name") or author_id)
    normalized = NormalizedMessage(
        channel="avito",
        external_conversation_id=chat_id,
        external_message_id=message_id,
        customer_ref=author_id,
        customer_name=customer_name,
        text=text,
        attachments={
            "avito": {
                "item_id": str(value.get("item_id") or ""),
                "chat_type": str(value.get("chat_type") or ""),
            }
        },
    )
    inbound = await _persist_avito_inbound(session, channel, event, event_id, value, normalized)
    return ChannelWebhookResponse(
        ok=True,
        channel_id=channel.id,
        conversation_id=inbound.conversation_id,
        inbound_message_id=inbound.id,
        processed_count=1,
    )


async def _persist_avito_inbound(
    session: AsyncSession,
    channel: Channel,
    event: WebhookEvent,
    event_id: str,
    value: dict[str, Any],
    normalized: NormalizedMessage,
) -> Message:
    customer = await _get_or_create_customer(session, channel, normalized)
    conversation = await _get_or_create_conversation(session, channel, customer, normalized)
    inbound = Message(
        tenant_id=channel.tenant_id,
        conversation_id=conversation.id,
        direction="inbound",
        sender_type="customer",
        text=normalized.text,
        attachments=normalized.attachments,
        external_message_id=normalized.external_message_id,
        status="received",
        ai_meta={
            "source": "avito",
            "chat_id": normalized.external_conversation_id,
            "avito_event_id": event_id,
            "item_id": str(value.get("item_id") or ""),
            "chat_type": str(value.get("chat_type") or ""),
        },
    )
    session.add(inbound)
    await session.flush()
    await session.commit()
    return inbound


async def send_avito_message(
    channel: Channel,
    chat_id: str,
    text: str,
) -> DeliveryResult:
    if len(text) > 1000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Avito message is too long")
    credentials = await _valid_credentials(channel)
    payload = await _api_request(
        "POST",
        "/messenger/v1/accounts/"
        f"{(channel.settings or {}).get('user_id')}/chats/{chat_id}/messages",
        access_token=credentials["access_token"],
        json_body={"message": {"text": text}, "type": "text"},
    )
    message_id = str(payload.get("id") or "")
    return DeliveryResult(
        delivered=bool(message_id),
        external_message_id=message_id or None,
        status="sent" if message_id else "failed",
        metadata={"delivery": "avito-messenger"},
    )


async def unsubscribe_avito_webhook(
    session: AsyncSession, channel: Channel
) -> None:
    path = str((channel.settings or {}).get("webhook_path") or "")
    if not path:
        return
    credentials = await _valid_credentials(channel, session=session)
    await _api_request(
        "POST",
        "/messenger/v1/webhook/unsubscribe",
        access_token=credentials["access_token"],
        json_body={"url": f"{settings.API_PUBLIC_URL.rstrip('/')}{path}"},
    )


async def poll_avito_channels(session: AsyncSession) -> dict[str, int]:
    """Fetch recent unread text messages as a webhook gap-repair fallback."""
    from app.services.channels.telegram import process_channel_inbound_message

    lease_cutoff = datetime.now(UTC) - AVITO_PROCESSING_LEASE
    pending_result = await session.execute(
        select(Message, WebhookEvent)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Channel, Channel.id == Conversation.channel_id)
        .join(
            WebhookEvent,
            (WebhookEvent.channel_id == Channel.id)
            & (
                (WebhookEvent.external_event_id == Message.external_message_id)
                | (
                    WebhookEvent.external_event_id
                    == literal("message:") + Message.external_message_id
                )
            ),
        )
        .where(
            Channel.type == "avito",
            Channel.status == "active",
            Message.direction == "inbound",
            Message.sender_type == "customer",
            WebhookEvent.processed.is_(False),
            or_(
                WebhookEvent.processing_started_at.is_(None),
                WebhookEvent.processing_started_at < lease_cutoff,
            ),
        )
        .order_by(WebhookEvent.created_at)
        .limit(100)
    )
    processed_pending = 0
    for inbound, event in pending_result.all():
        if not await _claim_avito_event(session, event.id, lease_cutoff):
            continue
        if str((inbound.ai_meta or {}).get("decision") or ""):
            event.processed = True
            event.processing_started_at = None
            await session.commit()
            continue
        try:
            await process_channel_inbound_message(session, inbound.id)
        except Exception:
            await session.rollback()
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.id == event.id, WebhookEvent.processed.is_(False))
                .values(processing_started_at=None)
            )
            await session.commit()
            raise
        await session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event.id)
            .values(processed=True, processing_started_at=None)
        )
        await session.commit()
        processed_pending += 1

    result = await session.execute(
        select(Channel).where(Channel.type == "avito", Channel.status == "active")
    )
    channels = list(result.scalars().all())
    ingested = 0
    for channel in channels:
        credentials = await _valid_credentials(channel, session=session)
        user_id = str((channel.settings or {}).get("user_id") or "")
        chats = await _api_request(
            "GET",
            f"/messenger/v2/accounts/{user_id}/chats?unread_only=true&limit=99&offset=0",
            access_token=credentials["access_token"],
        )
        for chat in chats.get("chats") or chats.get("resources") or []:
            if not isinstance(chat, dict):
                continue
            chat_id = str(chat.get("id") or "")
            if not chat_id:
                continue
            messages = await _api_request(
                "GET",
                f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/?limit=99&offset=0",
                access_token=credentials["access_token"],
            )
            for item in reversed(messages.get("messages") or messages.get("resources") or []):
                if not isinstance(item, dict):
                    continue
                message_id = str(item.get("id") or "")
                author_id = str(item.get("author_id") or "")
                content = item.get("content")
                text = str(content.get("text") or "").strip() if isinstance(content, dict) else ""
                if (
                    not message_id
                    or author_id == user_id
                    or str(item.get("type") or "") != "text"
                    or not text
                ):
                    continue
                if await _event_exists(session, channel.id, message_id, message_id):
                    continue
                event = WebhookEvent(
                    channel_id=channel.id,
                    external_event_id=message_id,
                    payload={"type": "text", "message_id": message_id, "chat_id": chat_id},
                    processed=False,
                )
                try:
                    async with session.begin_nested():
                        session.add(event)
                        await session.flush()
                except IntegrityError:
                    continue
                normalized = NormalizedMessage(
                    channel="avito",
                    external_conversation_id=chat_id,
                    external_message_id=message_id,
                    customer_ref=author_id,
                    customer_name=author_id,
                    text=text,
                )
                inbound = await _persist_avito_inbound(
                    session, channel, event, message_id, item, normalized
                )
                ingested += 1
                await process_channel_inbound_message(session, inbound.id)
                event.processed = True
                await session.commit()
    return {
        "channels": len(channels),
        "ingested": ingested,
        "processed_pending": processed_pending,
    }


async def _valid_credentials(
    channel: Channel, *, session: AsyncSession | None = None
) -> dict[str, str]:
    try:
        raw = json.loads(decrypt_secret(channel.credentials_encrypted))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Avito credentials are not configured"
        ) from exc
    access_token = str(raw.get("access_token") or "")
    refresh_token = str(raw.get("refresh_token") or "")
    try:
        expires_at = datetime.fromisoformat(str(raw.get("expires_at") or ""))
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Avito authorization expired") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if access_token and expires_at > datetime.now(UTC) + timedelta(seconds=60):
        return {"access_token": access_token}
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Avito authorization expired")
    if session is None:
        async with SessionLocal() as token_session:
            locked = (
                await token_session.execute(
                    select(Channel).where(Channel.id == channel.id).with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None or locked.type != "avito" or locked.status != "active":
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Avito authorization expired")
            return await _valid_credentials(locked, session=token_session)

    locked = (
        await session.execute(
            select(Channel)
            .where(Channel.id == channel.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Avito authorization expired")
    if locked is not channel:
        channel = locked
    try:
        raw = json.loads(decrypt_secret(channel.credentials_encrypted))
        access_token = str(raw.get("access_token") or "")
        refresh_token = str(raw.get("refresh_token") or "")
        expires_at = datetime.fromisoformat(str(raw.get("expires_at") or ""))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Avito authorization expired") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if access_token and expires_at > datetime.now(UTC) + timedelta(seconds=60):
        return {"access_token": access_token}
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Avito authorization expired")
    token = await _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": settings.AVITO_CLIENT_ID,
            "client_secret": settings.AVITO_CLIENT_SECRET,
            "refresh_token": refresh_token,
        }
    )
    new_access_token = str(token.get("access_token") or "")
    if not new_access_token:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Avito OAuth response")
    raw.update(
        access_token=new_access_token,
        refresh_token=str(token.get("refresh_token") or refresh_token),
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=max(int(token.get("expires_in") or 86400), 60))
        ).isoformat(),
    )
    channel.credentials_encrypted = encrypt_secret(json.dumps(raw, separators=(",", ":")))
    if session is not None:
        # Persist rotation before any later provider call can fail and roll back
        # the only valid refresh token.
        await session.commit()
    return {"access_token": str(raw["access_token"])}


async def _token_request(data: dict[str, str]) -> dict[str, Any]:
    if not settings.AVITO_CLIENT_ID or not settings.AVITO_CLIENT_SECRET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Avito OAuth is not configured")
    try:
        async with httpx.AsyncClient(timeout=settings.AVITO_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.post(f"{settings.AVITO_API_BASE_URL}/token", data=data)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Avito OAuth failed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Avito OAuth response")
    return payload


async def _api_request(
    method: str,
    path: str,
    *,
    access_token: str,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=settings.AVITO_DELIVERY_TIMEOUT_SEC) as client:
            response = await client.request(
                method,
                f"{settings.AVITO_API_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {access_token}"},
                json=json_body,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Avito API request failed") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid Avito API response")
    return payload


async def consume_avito_oauth_attempt(
    session: AsyncSession, state_token: str, browser_binding: str
) -> tuple[UUID, UUID]:
    if not state_token or not browser_binding:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Avito OAuth state")
    now = datetime.now(UTC)
    result = await session.execute(
        update(AvitoOAuthAttempt)
        .where(
            AvitoOAuthAttempt.state_hash == _secret_hash(state_token),
            AvitoOAuthAttempt.browser_binding_hash == _secret_hash(browser_binding),
            AvitoOAuthAttempt.consumed_at.is_(None),
            AvitoOAuthAttempt.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(AvitoOAuthAttempt.tenant_id, AvitoOAuthAttempt.user_id)
    )
    identity = result.first()
    await session.commit()
    if identity is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Avito OAuth state")
    return identity[0], identity[1]


_consume_oauth_attempt = consume_avito_oauth_attempt


def _secret_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


async def _claim_avito_event(
    session: AsyncSession, event_id: UUID, lease_cutoff: datetime
) -> bool:
    claimed = await session.execute(
        update(WebhookEvent)
        .where(
            WebhookEvent.id == event_id,
            WebhookEvent.processed.is_(False),
            or_(
                WebhookEvent.processing_started_at.is_(None),
                WebhookEvent.processing_started_at < lease_cutoff,
            ),
        )
        .values(processing_started_at=datetime.now(UTC))
    )
    await session.commit()
    return claimed.rowcount == 1


async def _channel_by_webhook_secret(session: AsyncSession, webhook_secret: str) -> Channel:
    result = await session.execute(
        select(Channel).where(
            Channel.type == "avito",
            Channel.status == "active",
            Channel.webhook_identity == _secret_hash(webhook_secret),
        )
    )
    channel = result.scalar_one_or_none()
    if channel is not None:
        return channel
    # One-time compatibility path for channels connected before migration 0010.
    legacy = await session.execute(
        select(Channel).where(
            Channel.type == "avito",
            Channel.status == "active",
            Channel.webhook_identity.is_(None),
        )
    )
    for channel in legacy.scalars().all():
        path = str((channel.settings or {}).get("webhook_path") or "")
        secret = path.rsplit("/", 1)[-1]
        if secret and secrets.compare_digest(secret, webhook_secret):
            channel.webhook_identity = _secret_hash(webhook_secret)
            await session.commit()
            return channel
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Active Avito channel not found")


async def _event_exists(
    session: AsyncSession, channel_id: UUID, event_id: str, message_id: str
) -> bool:
    result = await session.execute(
        select(WebhookEvent).where(
            WebhookEvent.channel_id == channel_id,
            WebhookEvent.external_event_id.in_(
                (event_id, message_id, f"message:{message_id}")
            ),
        )
    )
    return result.first() is not None


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
    customer = Customer(tenant_id=channel.tenant_id, display_name=message.customer_name or "Avito")
    identity = CustomerIdentity(
        customer_id=customer.id, channel_id=channel.id, external_user_id=message.customer_ref
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
    session: AsyncSession, channel: Channel, customer: Customer, message: NormalizedMessage
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
        return existing


def _channel_response(channel: Channel) -> ChannelResponse:
    safe_settings = {
        key: value
        for key, value in (channel.settings or {}).items()
        if key != "webhook_path"
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
