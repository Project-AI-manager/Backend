"""Knowledge base service.

For the MVP this module stores manual text documents and chunks in PostgreSQL.
Vector indexing/Qdrant can be attached later without changing the HTTP contract.
"""

import uuid
from collections.abc import Sequence

from fastapi import HTTPException, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log
from app.models.knowledge import KbCandidate, KbChunk, KbDocument
from app.models.tenant import TenantAIConfig
from app.schemas.knowledge import (
    KnowledgeCandidateApproveResponse,
    KnowledgeCandidateResponse,
    KnowledgeCandidateStatusResponse,
    KnowledgeChunkResponse,
    KnowledgeDocumentCreate,
    KnowledgeDocumentDetailResponse,
    KnowledgeDocumentResponse,
    KnowledgeDocumentStatusResponse,
)
from app.services.rag.embeddings import get_embedder
from app.services.rag.vector_store import VectorPoint, get_vector_store

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

    chunk_texts = split_text_into_chunks(body.text)
    chunks = await _add_chunks(session, tenant_id, document, chunk_texts, body.tags)
    await session.commit()
    await session.refresh(document)
    await _index_chunks(session=session, document=document, chunks=chunks)
    return _document_response(document, len(chunk_texts))


async def get_kb_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> KnowledgeDocumentDetailResponse:
    document = await _get_document(session, tenant_id, document_id)
    result = await session.execute(
        select(KbChunk)
        .where(KbChunk.tenant_id == tenant_id, KbChunk.document_id == document.id)
        .order_by(KbChunk.position)
    )
    chunks = [_chunk_response(chunk) for chunk in result.scalars().all()]
    base = _document_response(document, len(chunks))
    return KnowledgeDocumentDetailResponse(**base.model_dump(), chunks=chunks)


async def archive_kb_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> KnowledgeDocumentStatusResponse:
    document = await _get_document(session, tenant_id, document_id)
    vector_store = get_vector_store()
    if vector_store is not None:
        try:
            await vector_store.delete_document(tenant_id=tenant_id, document_id=document.id)
        except Exception as exc:  # noqa: BLE001 - SQL status remains the source of truth.
            log.warning(
                "knowledge_vector_delete_failed",
                error=str(exc),
                tenant_id=str(tenant_id),
                document_id=str(document.id),
            )
    document.status = "archived"
    await session.commit()
    await session.refresh(document)
    chunks_count = await _chunk_count(session, tenant_id, document.id)
    return KnowledgeDocumentStatusResponse(document=_document_response(document, chunks_count))


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

    chunks: list[KbChunk] = []
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
        chunks = await _add_chunks(
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
    if chunks:
        await _index_chunks(session=session, document=document, chunks=chunks)
    chunks_count = await _chunk_count(session, tenant_id, document.id)
    base = _candidate_response(candidate)
    return KnowledgeCandidateApproveResponse(
        **base.model_dump(),
        document=_document_response(document, chunks_count),
    )


async def reject_kb_candidate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> KnowledgeCandidateStatusResponse:
    candidate = await session.get(KbCandidate, candidate_id)
    if not candidate or candidate.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge candidate not found")

    candidate.status = "rejected"
    await session.commit()
    await session.refresh(candidate)
    base = _candidate_response(candidate)
    return KnowledgeCandidateStatusResponse(**base.model_dump())


async def _add_chunks(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document: KbDocument,
    chunks: Sequence[str],
    tags: dict[str, str],
) -> list[KbChunk]:
    created: list[KbChunk] = []
    for position, chunk_text in enumerate(chunks):
        chunk = KbChunk(
            tenant_id=tenant_id,
            document_id=document.id,
            text=chunk_text,
            position=position,
            token_count=len(chunk_text.split()),
            vector_id=f"kb:{document.id}:{position}",
            tags=tags,
            version=document.version,
        )
        session.add(chunk)
        created.append(chunk)
    await session.flush()
    return created


async def index_kb_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> int:
    """Replace one document's Qdrant points using the configured embedding contract."""
    document = await _get_document(session, tenant_id, document_id)
    if document.status != "ready":
        return 0
    result = await session.execute(
        select(KbChunk)
        .where(KbChunk.tenant_id == tenant_id, KbChunk.document_id == document_id)
        .order_by(KbChunk.position)
    )
    chunks = list(result.scalars().all())
    vector_store = get_vector_store()
    if vector_store is None:
        raise RuntimeError("Qdrant is disabled; set QDRANT_ENABLED=true before reindexing")
    # Build and validate replacement vectors before deleting the currently searchable points.
    # Qdrant upsert replaces stable chunk IDs, so deletion is unnecessary for current chunks.
    await _index_chunks(
        session=session,
        document=document,
        chunks=chunks,
        raise_errors=True,
    )
    return len(chunks)


async def reindex_ready_documents(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Reindex ready knowledge documents; return ``(documents, chunks)``."""
    query = select(KbDocument.id, KbDocument.tenant_id).where(KbDocument.status == "ready")
    if tenant_id is not None:
        query = query.where(KbDocument.tenant_id == tenant_id)
    rows = (await session.execute(query.order_by(KbDocument.created_at))).all()
    chunks = 0
    for document_id, row_tenant_id in rows:
        chunks += await index_kb_document(
            session,
            tenant_id=row_tenant_id,
            document_id=document_id,
        )
    return len(rows), chunks


async def _index_chunks(
    *,
    session: AsyncSession,
    document: KbDocument,
    chunks: Sequence[KbChunk],
    raise_errors: bool = False,
) -> None:
    vector_store = get_vector_store()
    if vector_store is None or not chunks:
        return

    try:
        ai_config = await session.get(TenantAIConfig, document.tenant_id)
        embedder = get_embedder(ai_config.embedding_model if ai_config else None)
        vectors = await embedder.embed(
            [f"{document.title}\n\n{chunk.text}\n\n{_tags_text(chunk.tags)}" for chunk in chunks]
        )
        await vector_store.upsert_chunks(
            [
                VectorPoint(
                    chunk_id=chunk.id,
                    vector_id=chunk.vector_id or str(chunk.id),
                    tenant_id=chunk.tenant_id,
                    document_id=chunk.document_id,
                    title=document.title,
                    text=chunk.text,
                    tags={str(key): str(value) for key, value in (chunk.tags or {}).items()},
                    version=chunk.version,
                    vector=vector,
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        )
    except Exception as exc:  # noqa: BLE001 - SQL knowledge base must remain usable.
        log.warning(
            "knowledge_vector_index_failed",
            error=str(exc),
            document_id=str(document.id),
            tenant_id=str(document.tenant_id),
        )
        if raise_errors:
            raise


async def reindex_kb_document(session: AsyncSession, document_id: uuid.UUID) -> int | None:
    """Replace one document's vector points and return the indexed chunk count.

    The operation is deliberately safe to retry: existing points are deleted by
    tenant/document filter and chunks keep stable point ids. ``None`` means that
    the document disappeared before a queued job started; archived documents are
    a successful no-op.
    """
    document = await session.get(KbDocument, document_id)
    if document is None:
        return None
    if document.status == "archived":
        return 0

    document.status = "processing"
    await session.commit()

    result = await session.execute(
        select(KbChunk)
        .where(
            KbChunk.tenant_id == document.tenant_id,
            KbChunk.document_id == document.id,
            KbChunk.version == document.version,
        )
        .order_by(KbChunk.position)
    )
    chunks = list(result.scalars().all())

    try:
        vector_store = get_vector_store()
        if vector_store is not None:
            await vector_store.delete_document(
                tenant_id=document.tenant_id,
                document_id=document.id,
            )
            if chunks:
                embedder = get_embedder()
                vectors = await embedder.embed(
                    [
                        f"{document.title}\n\n{chunk.text}\n\n{_tags_text(chunk.tags)}"
                        for chunk in chunks
                    ]
                )
                await vector_store.upsert_chunks(
                    [
                        VectorPoint(
                            chunk_id=chunk.id,
                            vector_id=chunk.vector_id or str(chunk.id),
                            tenant_id=chunk.tenant_id,
                            document_id=chunk.document_id,
                            title=document.title,
                            text=chunk.text,
                            tags={
                                str(key): str(value)
                                for key, value in (chunk.tags or {}).items()
                            },
                            version=chunk.version,
                            vector=vector,
                        )
                        for chunk, vector in zip(chunks, vectors, strict=True)
                    ]
                )
    except Exception:
        document.status = "failed"
        await session.commit()
        raise

    document.status = "ready"
    await session.commit()
    return len(chunks)


def _tags_text(tags: dict | None) -> str:
    return " ".join(str(value) for value in (tags or {}).values())


async def _chunk_count(session: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count(KbChunk.id)).where(
            KbChunk.tenant_id == tenant_id,
            KbChunk.document_id == document_id,
        )
    )
    return int(result.scalar_one())


async def _get_document(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> KbDocument:
    document = await session.get(KbDocument, document_id)
    if not document or document.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Knowledge document not found")
    return document


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


def _chunk_response(chunk: KbChunk) -> KnowledgeChunkResponse:
    return KnowledgeChunkResponse(
        id=chunk.id,
        document_id=chunk.document_id,
        text=chunk.text,
        position=chunk.position,
        token_count=chunk.token_count,
        tags=chunk.tags,
        version=chunk.version,
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
    """Compatibility entry point for workers and scripts."""
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        return await index_kb_document(
            session,
            tenant_id=uuid.UUID(tenant_id),
            document_id=uuid.UUID(document_id),
        )
