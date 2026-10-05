"""Experimental personal WhatsApp transport backed by a local whatsmeow bridge.

This keeps per-account device credentials in the Go bridge's dedicated SQLite
database. The application database stores only the channel ID and transport
metadata; incoming messages reuse the regular WhatsApp ingestion pipeline.
"""

from __future__ import annotations

import hmac
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.channel import Channel
from app.schemas.channels import ChannelResponse, ChannelWebhookResponse
from app.services.channels.base import DeliveryResult, NormalizedMessage
from app.services.channels.whatsapp import _ingest_message

TRANSPORT = "whatsmeow"


def _bridge_configured() -> tuple[str, str, str]:
    base_url = settings.WHATSAPP_PERSONAL_BRIDGE_URL.strip().rstrip("/")
    token = settings.WHATSAPP_PERSONAL_BRIDGE_TOKEN.strip()
    callback_token = settings.WHATSAPP_PERSONAL_CALLBACK_TOKEN.strip()
    if not base_url or not token or not callback_token:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Personal WhatsApp bridge is not configured",
        )
    if not base_url.startswith("http://127.0.0.1:") and not base_url.startswith(
        "http://localhost:"
    ):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Personal WhatsApp bridge must be available on localhost",
        )
    return base_url, token, callback_token


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    base_url, token, _ = _bridge_configured()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.request(
                method,
                f"{base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        code = (
            status.HTTP_409_CONFLICT
            if exc.response.status_code == 409
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(code, "Personal WhatsApp bridge request failed") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Personal WhatsApp bridge is unavailable",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Invalid bridge response")
    return payload


def _response(channel: Channel) -> ChannelResponse:
    return ChannelResponse(
        id=channel.id,
        type=channel.type,
        name=channel.name,
        status=channel.status,
        settings=dict(channel.settings or {}),
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


async def start_personal_qr(
    session: AsyncSession, tenant_id: UUID
) -> dict[str, Any]:
    """Create a distinct channel/device store and start QR pairing."""
    channel = Channel(
        tenant_id=tenant_id,
        type="whatsapp",
        name="WhatsApp (личный аккаунт)",
        status="disabled",
        settings={"transport": TRANSPORT, "auth_status": "qr_waiting"},
    )
    session.add(channel)
    await session.flush()
    channel.external_identity = f"whatsapp:personal:{channel.id}"
    try:
        payload = await _request(
            "POST", "/v1/sessions/qr/start", json={"channel_id": str(channel.id)}
        )
    except Exception:
        await session.rollback()
        raise
    auth_status = str(payload.get("status") or "waiting")
    channel.settings = {**channel.settings, "auth_status": auth_status}
    if auth_status == "active":
        channel.status = "active"
    elif auth_status == "error":
        channel.status = "error"
    await session.commit()
    await session.refresh(channel)
    return {"channel_id": str(channel.id), **payload}


async def get_personal_qr_status(
    session: AsyncSession, tenant_id: UUID, channel_id: UUID
) -> dict[str, Any]:
    channel = await _owned_personal_channel(session, tenant_id, channel_id)
    payload = await _request("GET", f"/v1/sessions/{channel.id}/status")
    auth_status = str(payload.get("status") or "error")
    channel.settings = {**channel.settings, "auth_status": auth_status}
    if auth_status == "active":
        channel.status = "active"
        account_id = str(payload.get("account_id") or "").strip()
        if account_id:
            channel.name = f"WhatsApp · {account_id}"
    elif auth_status in {"expired", "error", "disconnected"}:
        channel.status = "disabled"
    await session.commit()
    return {"channel_id": str(channel.id), **payload}


async def stop_personal_channel(
    session: AsyncSession, tenant_id: UUID, channel_id: UUID
) -> ChannelResponse:
    channel = await _owned_personal_channel(session, tenant_id, channel_id)
    await stop_personal_bridge_session(channel.id)
    channel.status = "disabled"
    channel.settings = {**channel.settings, "auth_status": "disconnected"}
    await session.commit()
    await session.refresh(channel)
    return _response(channel)


async def stop_personal_bridge_session(channel_id: UUID) -> None:
    await _request("POST", f"/v1/sessions/{channel_id}/stop")


async def send_personal_whatsapp_message(
    channel: Channel, recipient: str, text: str
) -> DeliveryResult:
    if (channel.settings or {}).get("transport") != TRANSPORT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a personal WhatsApp channel")
    payload = await _request(
        "POST",
        "/v1/messages/send",
        json={
            "channel_id": str(channel.id),
            "to": recipient,
            "text": text,
        },
    )
    return DeliveryResult(
        delivered=bool(payload.get("delivered")),
        external_message_id=str(payload.get("message_id") or "") or None,
        status=str(payload.get("status") or "sent"),
        metadata={"delivery": "whatsapp-personal", "transport": TRANSPORT},
    )


async def process_personal_whatsapp_inbound(
    session: AsyncSession,
    bearer_token: str | None,
    payload: dict[str, Any],
) -> ChannelWebhookResponse:
    """Accept one signed bridge event and send it through the normal AI pipeline."""
    _base_url, _api_token, expected_token = _bridge_configured()
    if not bearer_token or not hmac.compare_digest(bearer_token, expected_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bridge callback token")
    try:
        channel_id = UUID(str(payload.get("channel_id") or ""))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid channel_id") from exc
    channel = await session.get(Channel, channel_id)
    if (
        channel is None
        or channel.type != "whatsapp"
        or channel.status != "active"
        or (channel.settings or {}).get("transport") != TRANSPORT
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Active personal WhatsApp channel not found")
    event_id = str(payload.get("event_id") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    sender_id = str(payload.get("sender_id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not event_id or not chat_id or not sender_id or not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incomplete WhatsApp event")
    duplicate, result = await _ingest_message(
        session,
        channel,
        event_id,
        NormalizedMessage(
            channel="whatsapp",
            external_conversation_id=chat_id,
            external_message_id=event_id,
            customer_ref=sender_id,
            customer_name=str(payload.get("sender_name") or sender_id),
            text=text,
        ),
    )
    if duplicate:
        return ChannelWebhookResponse(ok=True, duplicate=True, channel_id=channel.id)
    await session.commit()
    result.channel_id = channel.id
    return result


async def _owned_personal_channel(
    session: AsyncSession, tenant_id: UUID, channel_id: UUID
) -> Channel:
    result = await session.execute(
        select(Channel).where(
            Channel.id == channel_id,
            Channel.tenant_id == tenant_id,
            Channel.type == "whatsapp",
        )
    )
    channel = result.scalar_one_or_none()
    if channel is None or (channel.settings or {}).get("transport") != TRANSPORT:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Personal WhatsApp channel not found")
    return channel
