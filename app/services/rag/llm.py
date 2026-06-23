"""LLM-провайдеры за единым интерфейсом — отделённый AI-слой (см. system-architecture).

Старт на заглушке MockLLM (доступов к YandexGPT/GigaChat пока нет).
"""
from abc import ABC, abstractmethod

from app.core.config import settings


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, context: list[str]) -> str: ...


class MockLLM(LLMProvider):
    async def generate(self, prompt: str, context: list[str]) -> str:
        return f"[mock-ответ на основе {len(context)} фрагментов базы знаний]"


class YandexGPTProvider(LLMProvider):
    async def generate(self, prompt: str, context: list[str]) -> str:
        raise NotImplementedError  # TODO: вызов Yandex AI Studio через httpx


class GigaChatProvider(LLMProvider):
    async def generate(self, prompt: str, context: list[str]) -> str:
        raise NotImplementedError  # TODO: вызов GigaChat через httpx


def get_llm() -> LLMProvider:
    return {"yandexgpt": YandexGPTProvider, "gigachat": GigaChatProvider}.get(
        settings.LLM_PROVIDER, MockLLM
    )()
