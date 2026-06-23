"""База знаний: документы, чанки, кандидаты автообучения. См. knowledge-base, rag-pipeline."""
import uuid

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class KbDocument(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "kb_document"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    source_type: Mapped[str] = mapped_column(String(16))  # pdf|docx|txt|md|manual|url
    storage_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="processing")  # processing|ready|failed|archived
    version: Mapped[int] = mapped_column(Integer, default=1)


class KbChunk(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "kb_chunk"
    __table_args__ = (
        UniqueConstraint("document_id", "position", "version", name="uq_kb_chunk_document_position_version"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_document.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    vector_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # → точка в Qdrant
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)


class KbCandidate(Base, UUIDMixin, TimestampMixin):
    """Кандидат из цикла автообучения: вопрос → ответ менеджера → подтверждение."""
    __tablename__ = "kb_candidate"
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversation.id"))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    suggested_by: Mapped[str] = mapped_column(String(16), default="manager")  # ai|manager
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|approved|rejected|merged
    resulting_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_document.id"), nullable=True
    )
