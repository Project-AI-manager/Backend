"""Эскалации, тарифы и учёт. См. confidence-and-escalation, saas-business-model."""

import uuid

from sqlalchemy import BigInteger, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, TimestampMixin, UUIDMixin


class Escalation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "escalation"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversation.id"), index=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(24))  # low_confidence|rule|manual|no_context
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|resolved
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)


class Plan(Base, UUIDMixin):
    __tablename__ = "plan"
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    price_month: Mapped[int] = mapped_column(Integer, default=0)
    dialog_limit: Mapped[int] = mapped_column(Integer, default=0)
    channel_limit: Mapped[int] = mapped_column(Integer, default=0)
    features: Mapped[dict] = mapped_column(JsonDict, default=dict)


class Subscription(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "subscription"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plan.id"))
    # trial|active|past_due|canceled
    status: Mapped[str] = mapped_column(String(16), default="trial")


class BillingAccount(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "billing_account"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_billing_account_tenant"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    balance_kopecks: Mapped[int] = mapped_column(Integer, default=100_000)


class UsageCounter(Base, UUIDMixin):
    __tablename__ = "usage_counter"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_counter_tenant_period"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    dialogs_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_replies_count: Mapped[int] = mapped_column(Integer, default=0)
    expenses_kopecks: Mapped[int] = mapped_column(Integer, default=0)


class AIUsageEvent(Base, UUIDMixin, TimestampMixin):
    """Immutable, per-generation accounting record for analytics and billing."""

    __tablename__ = "ai_usage_event"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer.id"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation.id"), nullable=True, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message.id"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    request_id: Mapped[str] = mapped_column(String(255), default="")
    reasoning_effort: Mapped[str] = mapped_column(String(16), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    provider_cost_microrubles: Mapped[int] = mapped_column(BigInteger, default=0)
    client_charge_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    currency_rate_kopecks: Mapped[int] = mapped_column(Integer, default=9000)
    outcome: Mapped[str] = mapped_column(String(24), default="completed")
    error_code: Mapped[str] = mapped_column(String(64), default="")
    metadata_json: Mapped[dict] = mapped_column(JsonDict, default=dict)


class AIDecisionEvent(Base, UUIDMixin, TimestampMixin):
    """Auditable decision even when no provider generation is performed."""

    __tablename__ = "ai_decision_event"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer.id"), nullable=True, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversation.id"), nullable=True, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("message.id"), nullable=True, index=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
