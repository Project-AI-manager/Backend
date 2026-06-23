"""RAG-пайплайн: вопрос → поиск в Qdrant → промпт → LLM → ответ + уверенность.

См. wiki/concepts/rag-pipeline.md и confidence-and-escalation.md.
"""
from app.services.confidence import compute_confidence
from app.services.rag.embeddings import get_embedder
from app.services.rag.llm import get_llm


async def answer(tenant_id: str, question: str, history: list[str] | None = None) -> dict:
    embedder = get_embedder()
    llm = get_llm()

    # 1. Эмбеддинг запроса
    [query_vec] = await embedder.embed([question])
    # 2. TODO: поиск top-k в Qdrant с фильтром tenant_id
    chunks: list[str] = []
    retrieval_score = 0.0
    # 3. Сборка промпта (строго по контексту) + 4. генерация
    text = await llm.generate(question, chunks)
    # 5. Метрика уверенности
    confidence = compute_confidence(retrieval_score=retrieval_score, coverage=0.0)
    return {"text": text, "confidence": confidence, "sources": chunks}
