"""LLM providers behind one interface, with deterministic mock mode by default."""

from abc import ABC, abstractmethod

from app.core.config import settings


class LLMProviderConfigurationError(RuntimeError):
    """Raised before generation when the configured provider cannot be used."""


class LLMProvider(ABC):
    provider_name = "base"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str: ...


class MockLLM(LLMProvider):
    """Grounded deterministic response generator used without API keys."""

    provider_name = "mock"

    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str:
        clean_context = [item.strip() for item in context if item.strip()]
        if not clean_context:
            return (
                "Спасибо за вопрос. Сейчас у меня недостаточно данных в базе знаний, "
                "поэтому я передам обращение менеджеру для точного ответа."
            )
        joined_context = " ".join(clean_context)
        return f"По базе знаний компании: {joined_context[:600]}"


class YandexGPTProvider(LLMProvider):
    provider_name = "yandexgpt"

    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str:
        raise NotImplementedError  # TODO: вызов Yandex AI Studio через httpx


class GigaChatProvider(LLMProvider):
    provider_name = "gigachat"

    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str:
        raise NotImplementedError  # TODO: вызов GigaChat через httpx


def get_llm(provider_name: str | None = None) -> LLMProvider:
    configured_name = (provider_name or settings.LLM_PROVIDER).strip().lower()
    if configured_name == "mock":
        return MockLLM()
    if configured_name in {"yandexgpt", "gigachat"}:
        raise LLMProviderConfigurationError(
            f"LLM provider '{configured_name}' is not available in local mock mode"
        )
    raise LLMProviderConfigurationError(f"Unsupported LLM provider '{configured_name}'")
