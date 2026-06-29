"""Unit tests for the ML message orchestration layer."""
import pytest

from app.services.ml.contracts import MemorySnippet, MLAnswerInput
from app.services.ml.memory import KeywordMemoryRetriever
from app.services.ml.service import MLMessageService


@pytest.mark.asyncio
async def test_ml_service_answers_with_memory_context() -> None:
    service = MLMessageService()

    result = await service.answer(
        MLAnswerInput(
            tenant_id="demo",
            message="iPhone 15 128ГБ есть в наличии и какая цена?",
            auto_reply_enabled=True,
            confidence_threshold=50,
        )
    )

    assert result.provider == "mock"
    assert result.sources
    assert result.confidence > 0
    assert result.decision == "auto_reply"
    assert "79 990" in result.answer


@pytest.mark.asyncio
async def test_ml_service_escalates_without_memory_context() -> None:
    service = MLMessageService(retriever=KeywordMemoryRetriever(snippets=[]))

    result = await service.answer(
        MLAnswerInput(
            tenant_id="demo",
            message="Какие условия корпоративного договора на 2027 год?",
            auto_reply_enabled=True,
            confidence_threshold=50,
        )
    )

    assert result.confidence == 0
    assert result.sources == ()
    assert result.decision == "escalate"
    assert "менеджер" in result.answer.lower()


@pytest.mark.asyncio
async def test_ml_service_accepts_api_memory_override() -> None:
    service = MLMessageService()

    result = await service.answer(
        MLAnswerInput(
            tenant_id="demo",
            message="Есть ли гарантия?",
            auto_reply_enabled=True,
            confidence_threshold=70,
            memory_override=(
                MemorySnippet(
                    id="manual-warranty",
                    title="Гарантия",
                    text="На новые устройства действует гарантия 12 месяцев.",
                    score=0.95,
                    source="test",
                ),
            ),
        )
    )

    assert result.sources[0].id == "manual-warranty"
    assert result.confidence >= 0.7
    assert result.decision == "auto_reply"
