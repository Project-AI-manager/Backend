"""ML message service: API/worker friendly orchestration entrypoint."""
from app.services.confidence import compute_confidence, should_auto_reply
from app.services.ml.contracts import MemorySnippet, MLAnswerInput, MLAnswerResult
from app.services.ml.memory import KeywordMemoryRetriever, MemoryRetriever
from app.services.ml.prompts import build_prompt
from app.services.rag.llm import LLMProvider, get_llm


class MLMessageService:
    """Coordinates memory retrieval, prompt assembly, LLM call and decisioning."""

    def __init__(
        self,
        *,
        retriever: MemoryRetriever | None = None,
        llm: LLMProvider | None = None,
    ) -> None:
        self.retriever = retriever or KeywordMemoryRetriever()
        self.llm = llm or get_llm()

    async def answer(self, request: MLAnswerInput) -> MLAnswerResult:
        memory = list(request.memory_override) or await self.retriever.retrieve(
            tenant_id=request.tenant_id,
            query=request.message,
        )
        prompt = build_prompt(
            message=request.message,
            profile=request.profile,
            memory=memory,
            history=request.history,
            custom_system_prompt=request.custom_system_prompt,
        )
        answer_text = await self.llm.generate(
            prompt.user_prompt,
            [snippet.text for snippet in memory],
            system_prompt=prompt.system_prompt,
            history=[f"{turn.role}: {turn.text}" for turn in request.history],
        )
        confidence = self._confidence(memory=memory, answer_text=answer_text)
        can_auto_reply = request.auto_reply_enabled and should_auto_reply(
            confidence,
            request.confidence_threshold,
        )
        decision = "auto_reply" if can_auto_reply else "escalate"
        return MLAnswerResult(
            answer=answer_text,
            confidence=confidence,
            decision=decision,
            sources=tuple(memory),
            provider=self.llm.provider_name,
            prompt=prompt,
        )

    @staticmethod
    def _confidence(*, memory: list[MemorySnippet], answer_text: str) -> float:
        if not memory:
            return 0.0
        retrieval_score = max(snippet.score for snippet in memory)
        coverage = min(1.0, len(memory) / 3)
        generation_ok = bool(answer_text.strip())
        return compute_confidence(
            retrieval_score=retrieval_score,
            coverage=coverage,
            generation_ok=generation_ok,
        )
