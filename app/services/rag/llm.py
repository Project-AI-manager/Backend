"""LLM-провайдеры за единым интерфейсом — отделённый AI-слой (см. system-architecture).

Старт на заглушке MockLLM (доступов к YandexGPT/GigaChat пока нет).
"""
import re
from abc import ABC, abstractmethod

from app.core.config import settings


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
    provider_name = "mock"

    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str:
        customer_question = self._extract_customer_question(prompt)
        question_lc = customer_question.lower()
        if not context:
            return (
                "Спасибо за вопрос. Сейчас у меня недостаточно данных в базе знаний, "
                "поэтому я передам обращение менеджеру для точного ответа."
            )

        joined_context = " ".join(context)
        if any(word in question_lc for word in ("рассроч", "кредит", "без переплат")):
            return (
                "По рассрочке нужно уточнить актуальные условия у менеджера: они зависят "
                "от текущей акции и банка-партнёра. Я передам вопрос специалисту."
            )
        if any(word in question_lc for word in ("iphone", "айфон", "налич", "цена")):
            return (
                "Да, iPhone 15 128ГБ есть в наличии в чёрном и синем цветах. "
                "Цена — 79 990 ₽. По Казани доступна доставка в день заказа."
            )
        if "достав" in question_lc:
            return (
                "Доставка по Казани доступна в день заказа, если оформить заказ до 17:00. "
                "Также можно забрать заказ самовывозом до 20:00."
            )
        return f"По базе знаний: {joined_context[:400]}"

    @staticmethod
    def _extract_customer_question(prompt: str) -> str:
        match = re.search(
            r"Вопрос клиента:\s*(?P<question>.*?)(?:\n\n|Сформируй ответ клиенту|$)",
            prompt,
            flags=re.DOTALL,
        )
        return match.group("question").strip() if match else prompt


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


def get_llm() -> LLMProvider:
    return {"yandexgpt": YandexGPTProvider, "gigachat": GigaChatProvider}.get(
        settings.LLM_PROVIDER, MockLLM
    )()
