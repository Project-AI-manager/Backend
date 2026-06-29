"""Knowledge base: documents, candidates and playground. Screen: /knowledge."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep
from app.api.v1.routes.ml import answer_message
from app.schemas.knowledge import (
    KnowledgeCandidateApproveResponse,
    KnowledgeCandidateResponse,
    KnowledgeDocumentCreate,
    KnowledgeDocumentResponse,
)
from app.schemas.ml import MLAnswerRequest, MLAnswerResponse
from app.services.knowledge import (
    approve_kb_candidate,
    create_kb_document,
    list_kb_candidates,
    list_kb_documents,
)

router = APIRouter()


def _tenant_id(user: CurrentUser) -> uuid.UUID:
    raw_tenant_id = user.get("tenant_id")
    if not raw_tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant is required")
    try:
        return uuid.UUID(str(raw_tenant_id))
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid tenant id") from exc


@router.get("/documents", response_model=list[KnowledgeDocumentResponse])
async def list_documents(
    user: CurrentUser,
    session: SessionDep,
) -> list[KnowledgeDocumentResponse]:
    return await list_kb_documents(session, _tenant_id(user))


@router.post("/documents", response_model=KnowledgeDocumentResponse)
async def upload_document(
    body: KnowledgeDocumentCreate,
    user: CurrentUser,
    session: SessionDep,
) -> KnowledgeDocumentResponse:
    return await create_kb_document(session, _tenant_id(user), body)


@router.post("/ask", response_model=MLAnswerResponse)
async def ask(body: MLAnswerRequest, user: CurrentUser) -> MLAnswerResponse:
    """Knowledge playground: same ML flow, exposed under knowledge for the UI."""
    return await answer_message(body, user)


@router.get("/candidates", response_model=list[KnowledgeCandidateResponse])
async def list_candidates(
    user: CurrentUser,
    session: SessionDep,
) -> list[KnowledgeCandidateResponse]:
    return await list_kb_candidates(session, _tenant_id(user))


@router.post("/candidates/{candidate_id}/approve", response_model=KnowledgeCandidateApproveResponse)
async def approve_candidate(
    candidate_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
) -> KnowledgeCandidateApproveResponse:
    return await approve_kb_candidate(session, _tenant_id(user), candidate_id)
