"""Knowledge base service.

For the MVP this module stores manual text documents and chunks in PostgreSQL.
Vector indexing/Qdrant can be attached later without changing the HTTP contract.
"""

import uuid
from collections.abc import Sequence

from fastapi import HTTPException, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.schemas.knowledge import (
    KnowledgeCandidateApproveResponse,
    KnowledgeCandidateResponse,
    KnowledgeDocumentCreate,
    KnowledgeDocumentResponse,
)

MAX_CHUNK_CHARS = 1200


def split_text_into_chunks(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    paragraphs = [part.strip() for part in text.replace("\r\n", "\n").split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                paragraph[start : start + max_chars].strip()
                for start in range(0, len(paragraph), max_chars)
                if paragraph[start : start + max_chars].strip()
            )
            continue

        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph

    if current:
        chunks.append(current)
    return chunks


async def list_kb_documents(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[KnowledgeDocumentResponse]:
    chunk_counts = (
        select(KbChunk.document_id, func.count(KbChunk.id).label("chunks_count"))
        .where(KbChunk.tenant_id == tenant_id)
        .group_by(KbChunk.document_id)
        .subquery()
    )
    result = await session.execute(
        select(KbDocument, func.coalesce(chunk_counts.c.chunks_count, 0))
        .outerjoin(chunk_counts, chunk_counts.c.document_id == KbDocument.id)
        .where(KbDocument.tenant_id == tenant_id)
        .order_by(desc(KbDocument.created_at))
    )
    return [
        _document_response(document, int(chunks_count))
        for document, chunks_count in result.all()
    ]


async def create_kb_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    body: KnowledgeDocumentCreate,
) -> KnowledgeDocumentResponse:
    document = KbDocument(
        tenant_id=tenant_id,
        title=body.title.strip(),
        source_type=body.source_type,
        storage_url=None,
        status="ready",
        version=1,
    )
    session.add(document)
    await session.flush()

    chunks = split_text_into_chunks(body.text)
    await _add_chunks(session, tenant_id, document, chunks, body.tags)
    await session.commit()
    await session.refresh(document)
    return _document_response(document, len(chunks))


async def list_kb_candidates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> list[KnowledgeCandidateResponse]:
    result = await session.execute(
        select(KbCandidate)
        .where(KbCandidate.tenant_id == tenant_id)
        .order_by(desc(KbCandidate.created_at))
    )
    return [_candidate_response(candidate) for candidate in result.scalars().all()]


async def approve_kb_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> KnowledgeCandidateApproveResponse:
    candidate = await session.get(KbCandidate, candidate_id)
    if not candidate or candidate.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge candidate not found")

    document: KbDocument | None = None
    if candidate.resulting_document_id:
        document = await session.get(KbDocument, candidate.resulting_document_id)

    if candidate.status != "approved" or document is None:
        document = KbDocument(
            tenant_id=tenant_id,
            title=_candidate_document_title(candidate.question),
            source_type="manual",
            storage_url=None,
            status="ready",
            version=1,
        )
        session.add(document)
        await session.flush()
        await _add_chunks(
            session,
            tenant_id,
            document,
            [f"Вопрос: {candidate.question}\n\nОтвет: {candidate.answer}"],
            {"source": "kb-candidate", "candidate_id": str(candidate.id)},
        )
        candidate.status = "approved"
        candidate.resulting_document_id = document.id

    await session.commit()
    await session.refresh(candidate)
    await session.refresh(document)
    chunks_count = await _chunk_count(session, tenant_id, document.id)
    base = _candidate_response(candidate)
    return KnowledgeCandidateApproveResponse(
        **base.model_dump(),
        document=_document_response(document, chunks_count),
    )


async def _add_chunks(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document: KbDocument,
    chunks: Sequence[str],
    tags: dict[str, str],
) -> None:
    for position, chunk_text in enumerate(chunks):
        session.add(
            KbChunk(
                tenant_id=tenant_id,
                document_id=document.id,
                text=chunk_text,
                position=position,
                token_count=len(chunk_text.split()),
                vector_id=f"kb:{document.id}:{position}",
                tags=tags,
                version=document.version,
            )
        )


async def _chunk_count(session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count(KbChunk.id)).where(
            KbChunk.tenant_id == tenant_id,
            KbChunk.document_id == document_id,
        )
    )
    return int(result.scalar_one())


def _document_response(document: KbDocument, chunks_count: int) -> KnowledgeDocumentResponse:
    return KnowledgeDocumentResponse(
        id=document.id,
        title=document.title,
        source_type=document.source_type,
        storage_url=document.storage_url,
        status=document.status,
        version=document.version,
        chunks_count=chunks_count,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _candidate_response(candidate: KbCandidate) -> KnowledgeCandidateResponse:
    return KnowledgeCandidateResponse(
        id=candidate.id,
        conversation_id=candidate.conversation_id,
        question=candidate.question,
        answer=candidate.answer,
        suggested_by=candidate.suggested_by,
        status=candidate.status,
        resulting_document_id=candidate.resulting_document_id,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def _candidate_document_title(question: str) -> str:
    normalized = " ".join(question.split())
    if len(normalized) <= 80:
        return f"Ответ из диалога: {normalized}"
    return f"Ответ из диалога: {normalized[:77]}..."


async def ingest_document(tenant_id: str, document_id: str) -> int:
    """Placeholder for future parser -> embedding -> Qdrant indexing."""
    raise NotImplementedError
