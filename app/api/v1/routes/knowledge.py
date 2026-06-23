"""База знаний: документы, кандидаты автообучения, playground. Экран: /knowledge."""
from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter()


@router.get("/documents")
async def list_documents(user: CurrentUser) -> list[dict]:
    return []  # TODO: список kb_document со статусом обработки


@router.post("/documents")
async def upload_document(user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: загрузка в S3 → задача индексации (ARQ)


@router.post("/ask")
async def ask(question: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: playground — RAG-ответ + источники + уверенность


@router.get("/candidates")
async def list_candidates(user: CurrentUser) -> list[dict]:
    return []  # TODO: очередь автообучения


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: str, user: CurrentUser) -> dict:
    raise NotImplementedError  # TODO: подтвердить → новый чанк → переиндексация
