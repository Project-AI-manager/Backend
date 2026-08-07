"""LLM providers behind one interface, with deterministic mock mode by default."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings


class LLMProviderConfigurationError(RuntimeError):
    """Raised before generation when the configured provider cannot be used."""


class LLMProviderRequestError(RuntimeError):
    """Raised when a configured provider cannot complete a generation request."""


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    provider_cost_usd: float = 0.0


@dataclass(frozen=True)
class LLMGeneration:
    text: str
    provider: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    request_id: str = ""


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

    async def generate_with_usage(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> LLMGeneration:
        return LLMGeneration(
            text=await self.generate(
                prompt,
                context,
                system_prompt=system_prompt,
                history=history,
            ),
            provider=self.provider_name,
            model="",
        )


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
        generation = await self.generate_with_usage(
            prompt,
            context,
            system_prompt=system_prompt,
            history=history,
        )
        return generation.text

    async def generate_with_usage(
        self,
        prompt: str,
        context: list[str],
        *,
        system_prompt: str = "",
        history: list[str] | None = None,
    ) -> LLMGeneration:
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
        return LLMGeneration(
            text=content,
            provider=self.provider_name,
            model=str(data.get("model") or self.model) if isinstance(data, dict) else self.model,
            usage=self._extract_usage(data),
            request_id=str(data.get("id") or "") if isinstance(data, dict) else "",
        )

    @staticmethod
    def _extract_usage(data: Any) -> LLMUsage:
        if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
            return LLMUsage()
        usage = data["usage"]
        input_tokens = OpenAICompatibleProvider._usage_int(usage, "prompt_tokens", "input_tokens")
        output_tokens = OpenAICompatibleProvider._usage_int(
            usage, "completion_tokens", "output_tokens"
        )
        input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        output_details = usage.get("completion_tokens_details") or usage.get(
            "output_tokens_details"
        )
        cached = OpenAICompatibleProvider._usage_int(input_details, "cached_tokens")
        cache_write = OpenAICompatibleProvider._usage_int(
            input_details, "cache_write_tokens", "cache_creation_tokens"
        )
        reasoning = OpenAICompatibleProvider._usage_int(output_details, "reasoning_tokens")
        total = OpenAICompatibleProvider._usage_int(usage, "total_tokens")
        return LLMUsage(
            input_tokens=input_tokens,
            cached_input_tokens=min(cached, input_tokens),
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            reasoning_tokens=min(reasoning, output_tokens),
            total_tokens=total or input_tokens + output_tokens,
            provider_cost_usd=OpenAICompatibleProvider._usage_float(
                usage, "cost", "response_cost", "total_cost"
            ),
        )

    @staticmethod
    def _usage_int(container: Any, *keys: str) -> int:
        if not isinstance(container, dict):
            return 0
        for key in keys:
            value = container.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, value)
        return 0

    @staticmethod
    def _usage_float(container: Any, *keys: str) -> float:
        if not isinstance(container, dict):
            return 0.0
        for key in keys:
            value = container.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, float(value))
        return 0.0

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
        response_usage: dict[str, Any] = {}
        response_model = ""
        response_id = ""
        response_trailers: dict[str, str] = {}

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith(":"):
                trailer_name, separator, trailer_value = (
                    line.removeprefix(":").strip().partition("=")
                )
                if separator and trailer_name.startswith("x-omniroute-"):
                    response_trailers[trailer_name] = trailer_value.strip()
                continue
            if not line.startswith("data:"):
                continue

            raw_payload = line.removeprefix("data:").strip()
            if not raw_payload or raw_payload == "[DONE]":
                continue

            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                continue

            last_payload = payload
            if isinstance(payload.get("usage"), dict):
                response_usage = payload["usage"]
            if isinstance(payload.get("model"), str):
                response_model = payload["model"]
            if isinstance(payload.get("id"), str):
                response_id = payload["id"]
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

        trailer_cost = response_trailers.get("x-omniroute-response-cost")
        if trailer_cost:
            try:
                response_usage["response_cost"] = max(0.0, float(trailer_cost))
            except ValueError:
                pass
        response_model = response_trailers.get("x-omniroute-model", response_model)
        response_id = response_trailers.get("x-omniroute-request-id", response_id)

        if content_parts:
            result: dict[str, Any] = {"choices": [{"message": {"content": "".join(content_parts)}}]}
            if response_usage:
                result["usage"] = response_usage
            if response_model:
                result["model"] = response_model
            if response_id:
                result["id"] = response_id
            return result

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
