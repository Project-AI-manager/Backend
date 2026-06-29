"""Pydantic schemas for the knowledge base API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

KnowledgeSourceType = Literal["manual", "txt", "md", "url"]


class KnowledgeDocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1)
    source_type: KnowledgeSourceType = "manual"
    tags: dict[str, str] = Field(default_factory=dict)


class KnowledgeDocumentResponse(BaseModel):
    id: UUID
    title: str
    source_type: str
    storage_url: str | None
    status: str
    version: int
    chunks_count: int
    created_at: datetime
    updated_at: datetime


class KnowledgeCandidateResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    question: str
    answer: str
    suggested_by: str
    status: str
    resulting_document_id: UUID | None
    created_at: datetime
    updated_at: datetime


class KnowledgeCandidateApproveResponse(KnowledgeCandidateResponse):
    document: KnowledgeDocumentResponse | None = None
