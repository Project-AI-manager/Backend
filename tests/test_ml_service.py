"""Unit tests for the ML message orchestration layer."""

import uuid

import pytest

from app.services.ml.contracts import ChatTurn, MemorySnippet, MLAnswerInput
from app.services.ml.memory import KeywordMemoryRetriever
from app.services.ml.service import MLMessageService
from app.services.rag.llm import (
    LLMProvider,
    LLMProviderConfigurationError,
    MockLLM,
    get_llm,
)

TENANT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


class CapturingLLM(LLMProvider):
    provider_name = "capturing"

    def __init__(self, answer: str = "Готовый ответ") -> None:
        self.answer = answer
        self.prompt = ""
        self.system_prompt = ""
        self.history: list[str] = []

    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str:
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.history = history or []
        return self.answer


def memory_snippet(*, risk: str = "") -> MemorySnippet:
    tags = {"risk": risk} if risk else {"topic": "telegram"}
    return MemorySnippet(
        id="telegram",
        title="Подключение Telegram",
        text="Подключение Telegram занимает 15 минут.",
        score=0.9,
        source="test",
        tags=tags,
    )


@pytest.mark.asyncio
async def test_ml_service_answers_with_memory_context() -> None:
    service = MLMessageService(llm=MockLLM())

    result = await service.answer(
        MLAnswerInput(
            tenant_id=TENANT_ID,
            message="Сколько занимает подключение Telegram?",
            auto_reply_enabled=True,
            confidence_threshold=50,
            memory_override=(memory_snippet(),),
        )
    )

    assert result.provider == "mock"
    assert result.sources
    assert result.confidence > 0
    assert result.decision == "auto_reply"
    assert "15 минут" in result.answer


@pytest.mark.asyncio
async def test_ml_service_escalates_without_context_even_at_zero_threshold() -> None:
    service = MLMessageService(
        retriever=KeywordMemoryRetriever(snippets=[]),
        llm=MockLLM(),
    )

    result = await service.answer(
        MLAnswerInput(
            tenant_id=TENANT_ID,
            message="Какие условия договора?",
            auto_reply_enabled=True,
            confidence_threshold=0,
        )
    )

    assert result.confidence == 0
    assert result.sources == ()
    assert result.decision == "escalate"
    assert "менеджер" in result.answer.lower()


@pytest.mark.asyncio
async def test_ml_service_escalates_when_auto_reply_is_disabled() -> None:
    service = MLMessageService(llm=MockLLM())

    result = await service.answer(
        MLAnswerInput(
            tenant_id=TENANT_ID,
            message="Как подключить Telegram?",
            auto_reply_enabled=False,
            confidence_threshold=0,
            memory_override=(memory_snippet(),),
        )
    )

    assert result.sources
    assert result.answer
    assert result.decision == "escalate"


@pytest.mark.asyncio
async def test_ml_service_escalates_for_manager_risk_context() -> None:
    service = MLMessageService(llm=MockLLM())

    result = await service.answer(
        MLAnswerInput(
            tenant_id=TENANT_ID,
            message="Можно подключить особые условия?",
            auto_reply_enabled=True,
            confidence_threshold=0,
            memory_override=(memory_snippet(risk="manager"),),
        )
    )

    assert result.confidence > 0
    assert result.decision == "escalate"


@pytest.mark.asyncio
async def test_ml_service_passes_only_last_eight_history_turns() -> None:
    llm = CapturingLLM()
    history = tuple(ChatTurn(role="customer", text=f"Сообщение {index}") for index in range(10))
    service = MLMessageService(llm=llm)

    result = await service.answer(
        MLAnswerInput(
            tenant_id=TENANT_ID,
            message="Текущий вопрос",
            history=history,
            custom_system_prompt="Не придумывай факты.",
            memory_override=(memory_snippet(),),
        )
    )

    assert len(llm.history) == 8
    assert "Сообщение 0" not in result.prompt.user_prompt
    assert "Сообщение 2" in result.prompt.user_prompt
    assert "Не придумывай факты." in llm.system_prompt


@pytest.mark.asyncio
async def test_ml_service_escalates_when_provider_returns_empty_answer() -> None:
    service = MLMessageService(llm=CapturingLLM(answer=""))

    result = await service.answer(
        MLAnswerInput(
            tenant_id=TENANT_ID,
            message="Текущий вопрос",
            auto_reply_enabled=True,
            confidence_threshold=0,
            memory_override=(memory_snippet(),),
        )
    )

    assert result.confidence > 0
    assert result.decision == "escalate"


def test_unknown_and_external_providers_fail_before_generation() -> None:
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("unknown")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("yandexgpt")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("gigachat")
    assert isinstance(get_llm("mock"), MockLLM)
