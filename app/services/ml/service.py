"""ML message service: API/worker friendly orchestration entrypoint."""

import re

from app.services.confidence import compute_confidence, should_auto_reply
from app.services.ml.contracts import Decision, MemorySnippet, MLAnswerInput, MLAnswerResult
from app.services.ml.memory import KeywordMemoryRetriever, MemoryRetriever
from app.services.ml.prompts import build_prompt
from app.services.rag.llm import LLMProvider, get_llm

_SOCIAL_MESSAGE_RE = re.compile(
    r"^(?:"
    r"привет(?:ствую)?|здравствуй(?:те)?|доброе утро|добрый день|добрый вечер|"
    r"hello|hi|hey"
    r")[!,.\s]*$",
    re.IGNORECASE,
)

_PROMPT_INJECTION_RE = re.compile(
    r"(?:ignore|forget|disregard).{0,30}(?:instructions?|system|prompt)|"
    r"(?:system\s*prompt|developer\s*message|jailbreak)|"
    r"(?:игнорируй|забудь|нарушь).{0,40}(?:инструкц|правил|промпт)|"
    r"(?:покажи|раскрой|выведи).{0,30}(?:системн(?:ый|ые)|промпт|инструкц)",
    re.IGNORECASE | re.DOTALL,
)

_OFF_TOPIC_RE = re.compile(
    r"(?:напиши|реши|сгенерируй|сочини|расскажи).{0,30}"
    r"(?:код|программ|стих|эссе|реферат|домашн|политик|рецепт|анекдот)|"
    r"(?:write|solve|generate|tell).{0,30}(?:code|poem|essay|homework|politics|recipe)",
    re.IGNORECASE | re.DOTALL,
)


class MLMessageService:
    """Coordinates memory retrieval, prompt assembly, LLM call and decisioning."""

    def __init__(
        self,
        *,
        retriever: MemoryRetriever | None = None,
        llm: LLMProvider | None = None,
    ) -> None:
        self.retriever = retriever or KeywordMemoryRetriever(snippets=[])
        self.llm = llm or get_llm()

    async def answer(self, request: MLAnswerInput) -> MLAnswerResult:
        safety_reason = self._safety_reason(request.message)
        if safety_reason:
            return MLAnswerResult(
                answer=(
                    "Я могу помочь только с вопросами о компании, её товарах и услугах. "
                    "Если у вас такой вопрос, напишите его, пожалуйста."
                ),
                confidence=0.0,
                decision="escalate",
                decision_reason=safety_reason,
                sources=(),
                provider="guardrail",
                prompt=build_prompt(
                    message=request.message,
                    profile=request.profile,
                    memory=[],
                    history=request.history,
                    custom_system_prompt=request.custom_system_prompt,
                ),
            )
        history = request.history
        memory = list(request.memory_override) or await self.retriever.retrieve(
            tenant_id=request.tenant_id,
            query=request.message,
        )
        social_message = self._is_social_message(request.message)
        prompt = build_prompt(
            message=request.message,
            profile=request.profile,
            memory=memory,
            history=history,
            custom_system_prompt=request.custom_system_prompt,
        )
        generation = await self.llm.generate_with_usage(
            prompt.user_prompt,
            [snippet.text for snippet in memory],
            system_prompt=prompt.system_prompt,
            history=[f"{turn.role}: {turn.text}" for turn in history],
        )
        answer_text = self._normalize_answer_style(generation.text)
        confidence = self._confidence(memory=memory, answer_text=answer_text)
        manager_rule = self._requires_manager(memory)
        can_auto_reply = (
            request.auto_reply_enabled
            and bool(answer_text.strip())
            and (
                social_message
                or (
                    bool(memory)
                    and confidence > 0
                    and not manager_rule
                    and self._passes_confidence_setting(
                        confidence=confidence,
                        threshold=request.confidence_threshold,
                    )
                )
            )
        )
        decision: Decision = "auto_reply" if can_auto_reply else "escalate"
        if can_auto_reply:
            decision_reason = "auto_reply_social" if social_message else "auto_reply_grounded"
        elif not request.auto_reply_enabled:
            decision_reason = "auto_reply_disabled"
        elif not answer_text.strip():
            decision_reason = "empty_answer"
        elif manager_rule:
            decision_reason = "manager_rule"
        elif not memory:
            decision_reason = "no_context"
        else:
            decision_reason = "low_confidence"
        return MLAnswerResult(
            answer=answer_text,
            confidence=confidence,
            decision=decision,
            sources=tuple(memory),
            provider=self.llm.provider_name,
            prompt=prompt,
            model=generation.model,
            request_id=generation.request_id,
            usage=generation.usage,
            decision_reason=decision_reason,
        )

    @staticmethod
    def _safety_reason(message: str) -> str | None:
        """Reject obvious instruction hijacking and unrelated assistant tasks pre-LLM."""
        if _PROMPT_INJECTION_RE.search(message):
            return "prompt_injection"
        if _OFF_TOPIC_RE.search(message):
            return "off_topic"
        return None

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

    @staticmethod
    def _requires_manager(memory: list[MemorySnippet]) -> bool:
        return any(
            snippet.tags.get("risk", "").casefold() in {"manager", "escalate"} for snippet in memory
        )

    @staticmethod
    def _passes_confidence_setting(*, confidence: float, threshold: int) -> bool:
        """Treat the UI percentage as the desired auto-reply share.

        A value of 100 means “reply whenever grounded knowledge exists”, while
        0 means “always hand off”. Internally this is the inverse of the
        minimum confidence accepted by ``should_auto_reply``.
        """
        minimum_confidence = 100 - max(0, min(100, threshold))
        return should_auto_reply(confidence, minimum_confidence)

    @staticmethod
    def _is_social_message(message: str) -> bool:
        """Allow a harmless greeting without inventing company facts.

        Knowledge grounding remains mandatory for factual answers. A standalone
        greeting is the one safe exception: escalating every ``привет`` makes a
        healthy assistant look unavailable and creates needless manager alerts.
        """
        return bool(_SOCIAL_MESSAGE_RE.fullmatch(message.strip()))

    @staticmethod
    def _normalize_answer_style(answer: str) -> str:
        """Keep model punctuation aligned with the conversational style contract."""

        def replace_dash(match: re.Match[str]) -> str:
            prefix = match.string[: match.start()]
            phrase = re.split(r"[.!?\n]", prefix)[-1].strip()
            words = phrase.split()
            separator = ": " if 0 < len(words) <= 3 and "," not in phrase else ", "
            return separator

        normalized = re.sub(r"\s*[—–]\s*", replace_dash, answer)
        normalized = re.sub(r",\s*([,.!?;:])", r"\1", normalized)
        return normalized.strip()
