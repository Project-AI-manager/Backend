"""Композитная метрика уверенности → решение авто-ответ / эскалация.

См. wiki/concepts/confidence-and-escalation.md. Веса — открытый вопрос, калибруем позже.
"""


def compute_confidence(*, retrieval_score: float, coverage: float, generation_ok: bool = True) -> float:
    """Возвращает 0..1. TODO: калибровка весов и сигналов генерации."""
    base = 0.6 * retrieval_score + 0.4 * coverage
    return round(base if generation_ok else base * 0.5, 3)


def should_auto_reply(confidence: float, threshold: int) -> bool:
    return confidence * 100 >= threshold
