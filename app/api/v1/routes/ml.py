"""ML message endpoint: message → memory → prompt → LLM → decision."""
from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.schemas.ml import MLAnswerRequest, MLAnswerResponse, MLSourceSchema
from app.services.ml.contracts import AssistantProfile, ChatTurn, MemorySnippet, MLAnswerInput
from app.services.ml.service import MLMessageService

router = APIRouter()


@router.post("/answer", response_model=MLAnswerResponse)
async def answer_message(body: MLAnswerRequest, user: CurrentUser) -> MLAnswerResponse:
    profile = AssistantProfile()
    if body.profile:
        rules = (
            tuple(body.profile.sales_rules)
            if body.profile.sales_rules
            else profile.sales_rules
        )
        profile = AssistantProfile(
            role_name=body.profile.role_name,
            company_name=body.profile.company_name,
            tone=body.profile.tone,
            language=body.profile.language,
            sales_rules=rules,
        )

    service = MLMessageService()
    result = await service.answer(
        MLAnswerInput(
            tenant_id=body.tenant_id,
            message=body.message,
            history=tuple(
                ChatTurn(role=turn.role, text=turn.text)
                for turn in body.history
            ),
            profile=profile,
            custom_system_prompt=body.custom_system_prompt,
            confidence_threshold=body.confidence_threshold,
            auto_reply_enabled=body.auto_reply_enabled,
            memory_override=tuple(
                MemorySnippet(
                    id=snippet.id,
                    title=snippet.title,
                    text=snippet.text,
                    score=snippet.score,
                    source=snippet.source,
                    tags=snippet.tags,
                )
                for snippet in body.memory
            ),
        )
    )
    return MLAnswerResponse(
        answer=result.answer,
        confidence=result.confidence,
        decision=result.decision,
        provider=result.provider,
        used_context=bool(result.sources),
        sources=[
            MLSourceSchema(
                id=source.id,
                title=source.title,
                text=source.text,
                score=source.score,
                source=source.source,
                tags=source.tags,
            )
            for source in result.sources
        ],
    )
