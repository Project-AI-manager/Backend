"""Клиенты и диалоги — ядро inbox. См. data-model: Группа 3."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class Customer(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "customer"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    note: Mapped[str] = mapped_column(Text, default="")


class CustomerIdentity(Base, UUIDMixin):
    """Привязка клиента к id в конкретном канале (узнаём человека across каналов)."""
    __tablename__ = "customer_identity"
    __table_args__ = (
        UniqueConstraint("channel_id", "external_user_id", name="uq_customer_identity_channel_external_user"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer.id"), index=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel.id"), index=True)
    external_user_id: Mapped[str] = mapped_column(String(255), index=True)


class Conversation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "conversation"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer.id"))
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel.id"))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|auto|escalated|closed|snoozed
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_preview: Mapped[str] = mapped_column(String(512), default="")
    unread_count: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_message_id", name="uq_message_conversation_external"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversation.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # inbound|outbound
    sender_type: Mapped[str] = mapped_column(String(16))  # customer|ai|manager|system
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    attachments: Mapped[dict] = mapped_column(JSONB, default=dict)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received|pending|sent|failed
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # использованные чанки, модель
