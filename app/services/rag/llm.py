"""LLM providers behind one interface, with deterministic mock mode by default."""

import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import settings


class LLMProviderConfigurationError(RuntimeError):
    """Raised before generation when the configured provider cannot be used."""


class LLMProviderRequestError(RuntimeError):
    """Raised when a configured provider cannot complete a generation request."""


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


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible chat completions provider, used by UniRouter locally."""

    provider_name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_sec: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_sec = timeout_sec

        missing = [
            name
            for name, value in (
                ("OPENAI_COMPATIBLE_BASE_URL", self.base_url),
                ("OPENAI_COMPATIBLE_API_KEY", self.api_key),
                ("OPENAI_COMPATIBLE_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            raise LLMProviderConfigurationError(
                "OpenAI-compatible provider is not configured: "
                + ", ".join(missing)
                + " is required"
            )

    async def generate(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(
                prompt=prompt,
                context=context,
                system_prompt=system_prompt,
                history=history or [],
            ),
            "temperature": 0.2,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_sec) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = self._parse_response(response)
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise LLMProviderRequestError("OpenAI-compatible provider request failed") from exc

        content = self._extract_content(data)
        if not content:
            raise LLMProviderRequestError("OpenAI-compatible provider returned an empty answer")
        return content

    @staticmethod
    def _messages(
        *,
        prompt: str,
        context: list[str],
        system_prompt: str,
        history: list[str],
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})

        clean_context = [item.strip() for item in context if item.strip()]
        if clean_context:
            messages.append(
                {
                    "role": "system",
                    "content": "Knowledge base context:\n" + "\n\n".join(clean_context),
                }
            )

        for turn in history:
            role, content = OpenAICompatibleProvider._history_turn(turn)
            if content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": prompt.strip()})
        return messages

    @staticmethod
    def _history_turn(turn: str) -> tuple[str, str]:
        raw_role, separator, raw_content = turn.partition(":")
        if not separator:
            return "user", turn.strip()

        role_name = raw_role.strip().lower()
        content = raw_content.strip()
        if role_name in {"manager", "ai", "assistant"}:
            return "assistant", content
        if role_name == "system":
            return "system", content
        return "user", content

    @staticmethod
    def _extract_content(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return ""
        message = first_choice.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ]
                return "\n".join(part.strip() for part in parts if part.strip())
        text = first_choice.get("text")
        return text.strip() if isinstance(text, str) else ""

    @staticmethod
    def _parse_response(response: httpx.Response) -> Any:
        text = response.text.strip()
        content_type = response.headers.get("content-type", "").lower()

        if "text/event-stream" in content_type or text.startswith("data:"):
            return OpenAICompatibleProvider._parse_sse_response(text)

        return response.json()

    @staticmethod
    def _parse_sse_response(text: str) -> dict[str, Any]:
        content_parts: list[str] = []
        last_payload: dict[str, Any] = {}

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue

            raw_payload = line.removeprefix("data:").strip()
            if not raw_payload or raw_payload == "[DONE]":
                continue

            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                continue

            last_payload = payload
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                continue

            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                continue

            delta = first_choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    content_parts.append(content)
                    continue

            message = first_choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    content_parts.append(content)

        if content_parts:
            return {"choices": [{"message": {"content": "".join(content_parts)}}]}

        return last_payload


def get_llm(provider_name: str | None = None) -> LLMProvider:
    configured_name = (provider_name or settings.LLM_PROVIDER).strip().lower()
    if configured_name == "mock":
        return MockLLM()
    if configured_name in {"openai", "openai-compatible", "unirouter"}:
        return OpenAICompatibleProvider(
            base_url=settings.OPENAI_COMPATIBLE_BASE_URL,
            api_key=settings.OPENAI_COMPATIBLE_API_KEY,
            model=settings.OPENAI_COMPATIBLE_MODEL,
            timeout_sec=settings.OPENAI_COMPATIBLE_TIMEOUT_SEC,
        )
    if configured_name in {"yandexgpt", "gigachat"}:
        raise LLMProviderConfigurationError(
            f"LLM provider '{configured_name}' is not available in local mock mode"
        )
    raise LLMProviderConfigurationError(f"Unsupported LLM provider '{configured_name}'")
