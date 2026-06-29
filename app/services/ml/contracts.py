"""Typed contracts for the ML message-processing pipeline.

The module intentionally does not depend on FastAPI or SQLAlchemy. It describes
the pure data flow that can be reused by API routes, workers and tests.
"""
from dataclasses import dataclass, field
from typing import Literal

Decision = Literal["auto_reply", "escalate"]
ChatRole = Literal["customer", "manager", "ai", "system"]


@dataclass(frozen=True)
class ChatTurn:
    role: ChatRole
    text: str


@dataclass(frozen=True)
class MemorySnippet:
    """A piece of retrieved company memory used as answer context."""

    id: str
    title: str
    text: str
    score: float
    source: str = "keyword-memory"
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AssistantProfile:
    """Stable sales-manager behaviour that is injected into every prompt."""

    role_name: str = "AI-менеджер по продажам"
    company_name: str = "компания клиента"
    tone: str = "профессионально, дружелюбно и уверенно"
    language: str = "русский"
    sales_rules: tuple[str, ...] = (
        "Отвечай кратко и по делу.",
        "Не обещай условия, которых нет в базе знаний.",
        "Если информации недостаточно, предложи уточнить у менеджера.",
        "Мягко подводи клиента к следующему шагу: покупка, бронь, консультация или контакт.",
        "Не выдумывай цены, сроки, наличие, гарантии и юридические условия.",
    )


@dataclass(frozen=True)
class PromptBundle:
    system_prompt: str
    user_prompt: str
    context_block: str


@dataclass(frozen=True)
class MLAnswerInput:
    tenant_id: str
    message: str
    history: tuple[ChatTurn, ...] = ()
    profile: AssistantProfile = field(default_factory=AssistantProfile)
    custom_system_prompt: str = ""
    confidence_threshold: int = 80
    auto_reply_enabled: bool = False
    memory_override: tuple[MemorySnippet, ...] = ()


@dataclass(frozen=True)
class MLAnswerResult:
    answer: str
    confidence: float
    decision: Decision
    sources: tuple[MemorySnippet, ...]
    provider: str
    prompt: PromptBundle

