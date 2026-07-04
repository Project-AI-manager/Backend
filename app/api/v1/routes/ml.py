"""ML message endpoint: message → memory → prompt → LLM → decision."""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.core.config import settings
from app.models.tenant import Tenant, TenantAIConfig
from app.schemas.ml import MLAnswerRequest, MLAnswerResponse, MLSourceSchema
from app.services.ml.contracts import AssistantProfile, ChatTurn, MLAnswerInput
from app.services.ml.memory import get_memory_retriever
from app.services.ml.service import MLMessageService
from app.services.rag.llm import LLMProviderConfigurationError, LLMProviderRequestError, get_llm

router = APIRouter()


@router.post("/answer", response_model=MLAnswerResponse)
async def answer_message(
    body: MLAnswerRequest,
    user: CurrentUser,
    session: SessionDep,
) -> MLAnswerResponse:
    tenant_id = tenant_id_from_user(user)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant not found")

    ai_config = await session.get(TenantAIConfig, tenant_id)
    provider_name = ai_config.llm_provider if ai_config else settings.LLM_PROVIDER
    try:
        llm = get_llm(provider_name)
    except LLMProviderConfigurationError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "llm_provider_unavailable",
                "message": str(exc),
            },
        ) from exc
    service = MLMessageService(
        retriever=get_memory_retriever(session),
        llm=llm,
    )
    try:
        result = await service.answer(
            MLAnswerInput(
                tenant_id=tenant_id,
                message=body.message,
                history=tuple(ChatTurn(role=turn.role, text=turn.text) for turn in body.history),
                profile=AssistantProfile(company_name=tenant.name),
                custom_system_prompt=ai_config.system_prompt if ai_config else "",
                confidence_threshold=ai_config.confidence_threshold if ai_config else 80,
                auto_reply_enabled=ai_config.auto_reply_enabled if ai_config else False,
            )
        )
    except LLMProviderRequestError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "llm_provider_request_failed",
                "message": str(exc),
            },
        ) from exc
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
