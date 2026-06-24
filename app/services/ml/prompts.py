"""Prompt assembly for sales-assistant LLM calls."""
from app.services.ml.contracts import AssistantProfile, ChatTurn, MemorySnippet, PromptBundle


def build_prompt(
    *,
    message: str,
    profile: AssistantProfile,
    memory: list[MemorySnippet],
    history: tuple[ChatTurn, ...] = (),
    custom_system_prompt: str = "",
) -> PromptBundle:
    context_block = build_context_block(memory)
    history_block = build_history_block(history)
    rules_block = "\n".join(f"- {rule}" for rule in profile.sales_rules)

    system_prompt = "\n".join(
        part
        for part in [
            f"Ты — {profile.role_name} для компании: {profile.company_name}.",
            f"Язык ответа: {profile.language}. Тон: {profile.tone}.",
            "Твоя задача — помогать клиенту купить или получить консультацию, "
            "но не выдумывать факты.",
            "Используй блок «Контекст из базы знаний» как источник правды.",
            "Если контекст не отвечает на вопрос, честно скажи, что уточнишь у менеджера.",
            "Правила продаж:",
            rules_block,
            custom_system_prompt.strip(),
        ]
        if part
    )

    user_prompt = "\n\n".join(
        part
        for part in [
            history_block,
            f"Контекст из базы знаний:\n{context_block}",
            f"Вопрос клиента:\n{message.strip()}",
            "Сформируй ответ клиенту. Не показывай внутренние рассуждения.",
        ]
        if part
    )
    return PromptBundle(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        context_block=context_block,
    )


def build_context_block(memory: list[MemorySnippet]) -> str:
    if not memory:
        return "Контекст не найден."
    return "\n\n".join(
        f"[{idx}] {snippet.title}\n{snippet.text}" for idx, snippet in enumerate(memory, start=1)
    )


def build_history_block(history: tuple[ChatTurn, ...]) -> str:
    if not history:
        return ""
    lines = ["История диалога:"]
    for turn in history[-8:]:
        lines.append(f"{turn.role}: {turn.text}")
    return "\n".join(lines)
