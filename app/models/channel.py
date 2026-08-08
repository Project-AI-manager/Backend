"""Каналы (telegram|web|avito|vk|max) и сырые вебхуки для идемпотентности."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonDict, TimestampMixin, UUIDMixin


class Channel(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "channel"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))  # web|avito|vk|max
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="disabled")  # active|disabled|error
    external_identity: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    webhook_identity: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="")  # шифруется (cryptography)
    settings: Mapped[dict] = mapped_column(JsonDict, default=dict)


class WebhookEvent(Base, UUIDMixin, TimestampMixin):
    """Сырое входящее событие канала — дедуп по (channel, external_event_id)."""

    __tablename__ = "webhook_event"
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "external_event_id",
            name="uq_webhook_event_channel_external",
        ),
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel.id"), index=True)
    external_event_id: Mapped[str] = mapped_column(String(255), index=True)
    payload: Mapped[dict] = mapped_column(JsonDict, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class AvitoOAuthAttempt(Base, UUIDMixin, TimestampMixin):
    """One-use, browser-bound Avito OAuth authorization attempt."""

    __tablename__ = "avito_oauth_attempt"

    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_binding_hash: Mapped[str] = mapped_column(String(64))
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
