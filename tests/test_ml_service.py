"""Unit tests for the ML message orchestration layer."""

import uuid
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.services.ml.contracts import ChatTurn, MemorySnippet, MLAnswerInput
from app.services.ml.memory import KeywordMemoryRetriever
from app.services.ml.service import MLMessageService
from app.services.rag.llm import (
    LLMProvider,
    LLMProviderConfigurationError,
    LLMProviderRequestError,
    MockLLM,
    OpenAICompatibleProvider,
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
async def test_ml_service_passes_all_history_turns() -> None:
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

    assert llm.history == [f"customer: Сообщение {index}" for index in range(10)]
    assert "Сообщение 0" in result.prompt.user_prompt
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


def test_unknown_and_external_providers_fail_before_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("unknown")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("yandexgpt")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("gigachat")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "")
    with pytest.raises(LLMProviderConfigurationError):
        get_llm("openai-compatible")
    assert isinstance(get_llm("mock"), MockLLM)


def test_get_llm_returns_openai_compatible_provider_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "http://localhost:20128/v1")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_MODEL", "cx/gpt-5.4-mini")

    provider = get_llm("unirouter")

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.provider_name == "openai-compatible"


@pytest.mark.asyncio
async def test_openai_compatible_provider_sends_chat_completion_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": "Ответ готов"}}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:20128/v1/",
        api_key="runtime-key",
        model="cx/gpt-5.4-mini",
        timeout_sec=12.0,
    )

    answer = await provider.generate(
        "Вопрос клиента",
        ["Контекст базы знаний"],
        system_prompt="Отвечай кратко",
        history=["customer: Привет", "manager: Добрый день"],
    )

    assert answer == "Ответ готов"
    assert captured["timeout"] == 12.0
    assert captured["url"] == "http://localhost:20128/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer runtime-key"
    assert captured["json"]["model"] == "cx/gpt-5.4-mini"
    assert captured["json"]["messages"][0] == {"role": "system", "content": "Отвечай кратко"}
    assert captured["json"]["messages"][-1] == {"role": "user", "content": "Вопрос клиента"}


@pytest.mark.asyncio
async def test_openai_compatible_provider_accepts_sse_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            body = "\n\n".join(
                [
                    (
                        'data: {"choices":[{"delta":{"role":"assistant",'
                        '"content":"Answer "},"finish_reason":null}]}'
                    ),
                    'data: {"choices":[{"delta":{"content":"ready"},"finish_reason":null}]}',
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                    "data: [DONE]",
                ]
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                request=httpx.Request("POST", url),
                content=body.encode(),
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:20128/v1",
        api_key="runtime-key",
        model="cx/gpt-5.4-mini",
        timeout_sec=12.0,
    )

    answer = await provider.generate("Question", [])

    assert answer == "Answer ready"


@pytest.mark.asyncio
async def test_openai_compatible_provider_raises_on_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, Any],
        ) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"choices": [{"message": {"content": ""}}]},
            )

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:20128/v1",
        api_key="runtime-key",
        model="cx/gpt-5.4-mini",
        timeout_sec=12.0,
    )

    with pytest.raises(LLMProviderRequestError):
        await provider.generate("Вопрос", [])
