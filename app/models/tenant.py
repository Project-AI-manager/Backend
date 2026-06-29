"""Тенант (компания-клиент) и его настройки AI. См. data-model: Группы 1 и 5."""
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class Tenant(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "tenant"
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|suspended


class TenantAIConfig(Base, TimestampMixin):
    """Настройки AI тенанта."""
    __tablename__ = "tenant_ai_config"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), primary_key=True)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_threshold: Mapped[int] = mapped_column(Integer, default=80)  # 0..100
    llm_provider: Mapped[str] = mapped_column(String(32), default="mock")
    embedding_model: Mapped[str] = mapped_column(String(64), default="multilingual-e5-large")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
