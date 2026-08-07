"""LLM usage normalization and ruble cost calculation."""

import re
from dataclasses import dataclass

from app.services.rag.llm import LLMUsage

USD_RATE_KOPECKS = 9_000
CLIENT_MARKUP = 3


@dataclass(frozen=True)
class UsageCost:
    provider_cost_microrubles: int
    client_charge_kopecks: int


def calculate_usage_cost(model: str, usage: LLMUsage) -> UsageCost:
    if usage.provider_cost_usd > 0:
        provider_cost_microrubles = round(usage.provider_cost_usd * USD_RATE_KOPECKS * 10_000)
        return UsageCost(
            provider_cost_microrubles,
            round(provider_cost_microrubles * CLIENT_MARKUP / 10_000),
        )
    input_usd, cached_usd, cache_write_usd, output_usd = _rates(model)
    uncached_input = max(0, usage.input_tokens - usage.cached_input_tokens)
    # OpenAI completion/output totals already include reasoning tokens.
    usd_millionths = round(
        uncached_input * input_usd
        + usage.cached_input_tokens * cached_usd
        + usage.cache_write_tokens * cache_write_usd
        + usage.output_tokens * output_usd
    )
    provider_cost_microrubles = round(usd_millionths * USD_RATE_KOPECKS / 100)
    client_charge_kopecks = round(provider_cost_microrubles * CLIENT_MARKUP / 10_000)
    return UsageCost(provider_cost_microrubles, client_charge_kopecks)


def reasoning_effort(model: str) -> str:
    match = re.search(r"-(low|medium|high|xhigh|max|ultra)$", model.casefold())
    return match.group(1) if match else ""


def _rates(model: str) -> tuple[float, float, float, float]:
    normalized = model.casefold()
    if "gpt-5.6-sol" in normalized:
        return 5.0, 0.5, 6.25, 30.0
    if "gpt-5.6-luna" in normalized:
        return 0.2, 0.02, 0.25, 1.2
    if "gpt-5.6-terra" in normalized:
        return 2.0, 0.2, 2.5, 12.0
    return 0.0, 0.0, 0.0, 0.0
