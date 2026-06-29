"""Эскалации, тарифы и учёт. См. confidence-and-escalation, saas-business-model."""
import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
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


class UsageCounter(Base, UUIDMixin):
    __tablename__ = "usage_counter"
    __table_args__ = (
        UniqueConstraint("tenant_id", "period", name="uq_usage_counter_tenant_period"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    dialogs_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_replies_count: Mapped[int] = mapped_column(Integer, default=0)
