"""Shared email notification flow for conversations that need a manager."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, Customer
from app.models.email import EmailOutbox
from app.services.email import ESCALATION_ALERT, send_escalation_alerts

ESCALATION_NOTIFICATION_COOLDOWN = timedelta(minutes=15)


async def notify_escalation_if_due(
    session: AsyncSession,
    conversation: Conversation,
    message_preview: str,
) -> bool:
    """Send one escalation alert per conversation within the cooldown window.

    ``EmailOutbox`` is used as the durable notification ledger, so the cooldown
    survives application restarts and works across multiple backend workers.
    """
    latest_result = await session.execute(
        select(EmailOutbox.created_at)
        .where(
            EmailOutbox.tenant_id == conversation.tenant_id,
            EmailOutbox.purpose == ESCALATION_ALERT,
            EmailOutbox.metadata_json["conversation_id"].as_string() == str(conversation.id),
        )
        .order_by(desc(EmailOutbox.created_at))
        .limit(1)
    )
    last_notified_at = latest_result.scalar_one_or_none()
    if last_notified_at is not None:
        if last_notified_at.tzinfo is None:
            last_notified_at = last_notified_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - last_notified_at < ESCALATION_NOTIFICATION_COOLDOWN:
            return False

    customer = await session.get(Customer, conversation.customer_id)
    await send_escalation_alerts(
        session,
        tenant_id=conversation.tenant_id,
        customer_name=customer.display_name if customer else "",
        message_preview=message_preview,
        conversation_id=conversation.id,
    )
    return True
