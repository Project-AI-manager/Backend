"""Импорт всех моделей — чтобы Base.metadata видел их при autogenerate Alembic."""

from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.email import EmailOutbox, EmailToken
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import (
    AIUsageEvent,
    BillingAccount,
    Escalation,
    Plan,
    Subscription,
    UsageCounter,
)
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import RefreshToken, User, UserNotificationSettings

__all__ = [
    "Tenant",
    "TenantAIConfig",
    "User",
    "UserNotificationSettings",
    "RefreshToken",
    "EmailToken",
    "EmailOutbox",
    "Channel",
    "WebhookEvent",
    "Customer",
    "CustomerIdentity",
    "Conversation",
    "Message",
    "KbDocument",
    "KbChunk",
    "KbCandidate",
    "Escalation",
    "Plan",
    "Subscription",
    "BillingAccount",
    "UsageCounter",
    "AIUsageEvent",
]
