from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.models.channel import Channel
from app.schemas.channels import ChannelWebhookResponse
from app.services.channels import whatsapp_personal
from app.services.channels.telegram import _deliver_telegram_reply


@pytest.fixture(autouse=True)
def bridge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_PERSONAL_BRIDGE_URL", "http://127.0.0.1:8092")
    monkeypatch.setattr(settings, "WHATSAPP_PERSONAL_BRIDGE_TOKEN", "test-bridge-token")
    monkeypatch.setattr(settings, "WHATSAPP_PERSONAL_CALLBACK_TOKEN", "test-callback-token")


@pytest.mark.asyncio
async def test_personal_sender_uses_bridge_and_channel_specific_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock(
        return_value={"delivered": True, "message_id": "wamid-test", "status": "sent"}
    )
    monkeypatch.setattr(whatsapp_personal, "_request", request)
    channel = Channel(
        id=uuid4(),
        tenant_id=uuid4(),
        type="whatsapp",
        status="active",
        settings={"transport": "whatsmeow"},
    )

    result = await whatsapp_personal.send_personal_whatsapp_message(
        channel, "15551234567@s.whatsapp.net", "Привет"
    )

    request.assert_awaited_once_with(
        "POST",
        "/v1/messages/send",
        json={
            "channel_id": str(channel.id),
            "to": "15551234567@s.whatsapp.net",
            "text": "Привет",
        },
    )
    assert result.delivered is True
    assert result.external_message_id == "wamid-test"
    assert result.metadata["transport"] == "whatsmeow"


@pytest.mark.asyncio
async def test_channel_reply_routes_personal_whatsapp_through_personal_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = AsyncMock(
        return_value=whatsapp_personal.DeliveryResult(
            delivered=True,
            external_message_id="wamid-handoff",
            status="sent",
            metadata={"delivery": "whatsapp-personal", "transport": "whatsmeow"},
        )
    )
    monkeypatch.setattr(whatsapp_personal, "send_personal_whatsapp_message", send)
    channel = Channel(
        id=uuid4(),
        tenant_id=uuid4(),
        type="whatsapp",
        status="active",
        settings={"transport": "whatsmeow"},
    )

    delivered, receipt, message_id = await _deliver_telegram_reply(
        channel, "15551234567@s.whatsapp.net", "Менеджер скоро ответит"
    )

    send.assert_awaited_once_with(
        channel, "15551234567@s.whatsapp.net", "Менеджер скоро ответит"
    )
    assert delivered is True
    assert receipt == "whatsapp-personal"
    assert message_id == "wamid-handoff"


@pytest.mark.asyncio
async def test_personal_inbound_rejects_invalid_callback_token() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await whatsapp_personal.process_personal_whatsapp_inbound(
            AsyncMock(), "wrong-token", {}
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_personal_inbound_reuses_whatsapp_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = Channel(
        id=uuid4(),
        tenant_id=uuid4(),
        type="whatsapp",
        status="active",
        settings={"transport": "whatsmeow"},
    )
    session = AsyncMock()
    session.get.return_value = channel
    ingest = AsyncMock(
        return_value=(False, ChannelWebhookResponse(ok=True, processed_count=1))
    )
    monkeypatch.setattr(whatsapp_personal, "_ingest_message", ingest)

    response = await whatsapp_personal.process_personal_whatsapp_inbound(
        session,
        "test-callback-token",
        {
            "channel_id": str(channel.id),
            "event_id": "chat:msg-1",
            "chat_id": "15551234567@s.whatsapp.net",
            "sender_id": "15551234567@s.whatsapp.net",
            "sender_name": "Customer",
            "text": "Hello",
        },
    )

    assert response.processed_count == 1
    assert ingest.await_args.args[1] is channel
    assert ingest.await_args.args[2] == "chat:msg-1"
    normalized = ingest.await_args.args[3]
    assert normalized.external_conversation_id == "15551234567@s.whatsapp.net"
    assert normalized.text == "Hello"
    session.commit.assert_awaited_once()


def test_personal_bridge_refuses_non_loopback_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_PERSONAL_BRIDGE_URL", "http://bridge.internal:8092")

    with pytest.raises(HTTPException) as exc_info:
        whatsapp_personal._bridge_configured()

    assert exc_info.value.status_code == 503
