"""ML message endpoint: message → memory → prompt → LLM → decision."""

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import CurrentUser, SessionDep, tenant_id_from_user
from app.core.config import settings
from app.models.tenant import Tenant, TenantAIConfig
from app.schemas.ml import MLAnswerRequest, MLAnswerResponse, MLSourceSchema
from app.services.billing.ledger import record_llm_attempt
from app.services.guardrails.rate_limit import acquire_tenant_llm_slot, burst_limiter
from app.services.ml.contracts import AssistantProfile, ChatTurn, MLAnswerInput
from app.services.ml.memory import get_memory_retriever
from app.services.ml.service import MLMessageService
from app.services.rag.llm import LLMProviderConfigurationError, LLMProviderRequestError, get_llm

router = APIRouter()


@router.post("/answer", response_model=MLAnswerResponse)
async def answer_message(
    body: MLAnswerRequest,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> MLAnswerResponse:
    return await run_answer_message(body, user, session, request=request)


async def run_answer_message(
    body: MLAnswerRequest,
    user: dict[str, object],
    session: SessionDep,
    *,
    request: Request | None = None,
) -> MLAnswerResponse:
    tenant_id = tenant_id_from_user(user)
    db_user = user.get("db_user")
    user_id = str(getattr(db_user, "id", user.get("sub", "unknown")))
    client_ip = request.client.host if request and request.client else "unknown"
    allowed = await burst_limiter.allow(
        f"ml:{tenant_id}:{user_id}:{client_ip}",
        limit=settings.ML_RATE_LIMIT_PER_MINUTE,
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limit_exceeded",
                "message": "Слишком много запросов. Повторите через минуту.",
            },
        )
    budget_reason = await acquire_tenant_llm_slot(session, tenant_id)
    if budget_reason:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": budget_reason, "message": "Лимит AI-запросов временно исчерпан."},
        )
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
        retriever=await get_memory_retriever(session, tenant_id),
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
        await record_llm_attempt(
            session,
            tenant_id=tenant_id,
            provider=provider_name,
            outcome="error",
            error_code=type(exc).__name__,
            metadata={"surface": "ml_api"},
        )
        await session.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "llm_provider_request_failed",
                "message": str(exc),
            },
        ) from exc
    if result.provider != "guardrail":
        await record_llm_attempt(
            session,
            tenant_id=tenant_id,
            provider=result.provider,
            model=result.model,
            usage=result.usage,
            request_id=result.request_id,
            outcome="completed" if result.decision == "auto_reply" else "escalated",
            metadata={"surface": "ml_api", "decision_reason": result.decision_reason},
        )
        await session.commit()
    return MLAnswerResponse(
        answer=result.answer,
        confidence=result.confidence,
        decision=result.decision,
        provider=result.provider,
        decision_reason=result.decision_reason,
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
