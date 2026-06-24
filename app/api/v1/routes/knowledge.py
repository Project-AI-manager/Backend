"""База знаний: документы, кандидаты автообучения, playground. Экран: /knowledge."""
from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.api.v1.routes.ml import answer_message
from app.schemas.ml import MLAnswerRequest, MLAnswerResponse

router = APIRouter()


@router.get("/documents")
async def list_documents(user: CurrentUser) -> list[dict]:
    return []  # TODO: список kb_document со статусом обработки


@router.post("/documents")
async def upload_document(user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: загрузка в S3 → задача индексации (ARQ)


@router.post("/ask", response_model=MLAnswerResponse)
async def ask(body: MLAnswerRequest, user: CurrentUser) -> MLAnswerResponse:
    """Knowledge playground: same ML flow, exposed under knowledge for the UI."""
    return await answer_message(body, user)


@router.get("/candidates")
async def list_candidates(user: CurrentUser) -> list[dict]:
    return []  # TODO: очередь автообучения


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: подтвердить → новый чанк → переиндексация
