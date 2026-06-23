"""Импорт всех моделей — чтобы Base.metadata видел их при autogenerate Alembic."""
from app.models.channel import Channel, WebhookEvent
from app.models.conversation import Conversation, Customer, CustomerIdentity, Message
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.ops import Escalation, Plan, Subscription, UsageCounter
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import RefreshToken, User

__all__ = [
    "Tenant", "TenantAIConfig", "User", "RefreshToken", "Channel", "WebhookEvent",
    "Customer", "CustomerIdentity", "Conversation", "Message",
    "KbDocument", "KbChunk", "KbCandidate",
    "Escalation", "Plan", "Subscription", "UsageCounter",
]
