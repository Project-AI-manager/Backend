"""Analytics dashboard endpoints. Screen: /analytics."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.models.conversation import Conversation, Message
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import Plan, Subscription, UsageCounter
from app.schemas.analytics import AnalyticsOverviewResponse, AnalyticsStatusBreakdownItem

router = APIRouter()


@router.get("/overview", response_model=AnalyticsOverviewResponse)
async def overview(user: CurrentUser, session: SessionDep) -> AnalyticsOverviewResponse:
    tenant_id = tenant_id_from_user(user)

    conversations = await _tenant_conversations(session, tenant_id)
    messages = await _tenant_messages(session, tenant_id)
    dialogs_used, dialogs_limit = await _usage_and_limit(session, tenant_id)
    knowledge_documents_ready, knowledge_chunks_count, pending_candidates_count = (
        await _knowledge_counts(session, tenant_id)
    )

    status_counts = _status_counts(conversations)
    dialogs_total = len(conversations)
    ai_replies = [message for message in messages if message.sender_type == "ai"]
    manager_replies = [message for message in messages if message.sender_type == "manager"]
    inbound_messages = [message for message in messages if message.direction == "inbound"]
    conversations_with_ai = {
        message.conversation_id for message in ai_replies if message.conversation_id is not None
    }
    confidence_values = [
        message.confidence
        for message in ai_replies
        if isinstance(message.confidence, (int, float))
    ]

    return AnalyticsOverviewResponse(
        dialogs_total=dialogs_total,
        dialogs_open=status_counts.get("open", 0) + status_counts.get("answered", 0),
        dialogs_auto=status_counts.get("auto", 0),
        dialogs_escalated=status_counts.get("escalated", 0),
        dialogs_closed=status_counts.get("closed", 0),
        auto_reply_rate=_ratio(len(conversations_with_ai), dialogs_total),
        escalation_rate=_ratio(status_counts.get("escalated", 0), dialogs_total),
        avg_response_sec=_average_response_sec(messages),
        avg_ai_confidence=round(sum(confidence_values) / len(confidence_values), 4)
        if confidence_values
        else 0.0,
        ai_replies_count=len(ai_replies),
        manager_replies_count=len(manager_replies),
        inbound_messages_count=len(inbound_messages),
        dialogs_used=dialogs_used,
        dialogs_limit=dialogs_limit,
        knowledge_documents_ready=knowledge_documents_ready,
        knowledge_chunks_count=knowledge_chunks_count,
        pending_candidates_count=pending_candidates_count,
        status_breakdown=[
            AnalyticsStatusBreakdownItem(status=status, count=count)
            for status, count in sorted(status_counts.items())
        ],
    )


async def _tenant_conversations(session: SessionDep, tenant_id: UUID) -> list[Conversation]:
    result = await session.execute(
        select(Conversation).where(Conversation.tenant_id == tenant_id)
    )
    return list(result.scalars().all())


async def _tenant_messages(session: SessionDep, tenant_id: UUID) -> list[Message]:
    result = await session.execute(
        select(Message).where(Message.tenant_id == tenant_id).order_by(Message.created_at)
    )
    return list(result.scalars().all())


async def _usage_and_limit(session: SessionDep, tenant_id: UUID) -> tuple[int, int]:
    current_period = datetime.now(UTC).strftime("%Y-%m")
    usage_result = await session.execute(
        select(UsageCounter).where(
            UsageCounter.tenant_id == tenant_id,
            UsageCounter.period == current_period,
        )
    )
    usage = usage_result.scalar_one_or_none()

    subscription_result = await session.execute(
        select(Subscription, Plan)
        .join(Plan, Subscription.plan_id == Plan.id)
        .where(Subscription.tenant_id == tenant_id)
        .order_by(Subscription.created_at.desc())
    )
    subscription_row = subscription_result.first()
    dialogs_limit = subscription_row[1].dialog_limit if subscription_row else 0
    return usage.dialogs_count if usage else 0, dialogs_limit


async def _knowledge_counts(session: SessionDep, tenant_id: UUID) -> tuple[int, int, int]:
    documents_result = await session.execute(
        select(KbDocument).where(
            KbDocument.tenant_id == tenant_id,
            KbDocument.status == "ready",
        )
    )
    chunks_result = await session.execute(
        select(KbChunk).where(KbChunk.tenant_id == tenant_id)
    )
    candidates_result = await session.execute(
        select(KbCandidate).where(
            KbCandidate.tenant_id == tenant_id,
            KbCandidate.status == "pending",
        )
    )
    return (
        len(documents_result.scalars().all()),
        len(chunks_result.scalars().all()),
        len(candidates_result.scalars().all()),
    )


def _status_counts(conversations: list[Conversation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for conversation in conversations:
        counts[conversation.status] = counts.get(conversation.status, 0) + 1
    return counts


def _average_response_sec(messages: list[Message]) -> int:
    by_conversation: dict[UUID, list[Message]] = {}
    for message in messages:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    response_times: list[float] = []
    for conversation_messages in by_conversation.values():
        pending_inbound_at: datetime | None = None
        for message in conversation_messages:
            if message.direction == "inbound":
                pending_inbound_at = message.created_at
                continue
            if (
                pending_inbound_at is not None
                and message.direction == "outbound"
                and message.sender_type in {"ai", "manager"}
            ):
                response_times.append(
                    max(0.0, (message.created_at - pending_inbound_at).total_seconds())
                )
                pending_inbound_at = None

    if not response_times:
        return 0
    return round(sum(response_times) / len(response_times))


def _ratio(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(value / total, 4)
