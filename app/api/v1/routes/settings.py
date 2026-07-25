"""Tenant settings: AI, billing and workspace profile. Screens: /settings/*."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser, CurrentUser, SessionDep, tenant_id_from_user
from app.core.config import settings
from app.models.ops import Plan, Subscription, UsageCounter
from app.models.tenant import Tenant, TenantAIConfig
from app.schemas.settings import (
    LLM_PROVIDERS,
    AISettingsResponse,
    AISettingsUpdate,
    BillingSettingsResponse,
    WorkspaceSettingsResponse,
    WorkspaceSettingsUpdate,
)

router = APIRouter()

@router.get("/ai", response_model=AISettingsResponse)
async def get_ai_settings(user: CurrentUser, session: SessionDep) -> AISettingsResponse:
    config = await _get_or_create_ai_config(session, tenant_id_from_user(user))
    return _ai_settings_response(config)


@router.put("/ai", response_model=AISettingsResponse)
async def update_ai_settings(
    body: AISettingsUpdate,
    user: AdminUser,
    session: SessionDep,
) -> AISettingsResponse:
    config = await _get_or_create_ai_config(session, tenant_id_from_user(user))

    if body.auto_reply_enabled is not None:
        config.auto_reply_enabled = body.auto_reply_enabled
    if body.confidence_threshold is not None:
        config.confidence_threshold = body.confidence_threshold
    if body.llm_provider is not None:
        provider = body.llm_provider.strip().lower()
        if provider not in LLM_PROVIDERS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "code": "unsupported_llm_provider",
                    "message": f"Unsupported LLM provider '{provider}'",
                    "available_providers": list(LLM_PROVIDERS),
                },
            )
        config.llm_provider = provider
    if body.embedding_model is not None:
        config.embedding_model = body.embedding_model.strip()
    if body.system_prompt is not None:
        config.system_prompt = body.system_prompt.strip()

    await session.commit()
    await session.refresh(config)
    return _ai_settings_response(config)


@router.get("/workspace", response_model=WorkspaceSettingsResponse)
async def get_workspace_settings(
    user: CurrentUser,
    session: SessionDep,
) -> WorkspaceSettingsResponse:
    tenant = await _get_tenant(session, tenant_id_from_user(user))
    return _workspace_settings_response(tenant)


@router.put("/workspace", response_model=WorkspaceSettingsResponse)
async def update_workspace_settings(
    body: WorkspaceSettingsUpdate,
    user: AdminUser,
    session: SessionDep,
) -> WorkspaceSettingsResponse:
    tenant = await _get_tenant(session, tenant_id_from_user(user))
    tenant.name = body.name.strip()
    await session.commit()
    await session.refresh(tenant)
    return _workspace_settings_response(tenant)


@router.get("/billing", response_model=BillingSettingsResponse)
async def get_billing(user: CurrentUser, session: SessionDep) -> BillingSettingsResponse:
    tenant_id = tenant_id_from_user(user)
    subscription_result = await session.execute(
        select(Subscription, Plan)
        .join(Plan, Subscription.plan_id == Plan.id)
        .where(Subscription.tenant_id == tenant_id)
        .order_by(Subscription.created_at.desc())
    )
    subscription_row = subscription_result.first()

    current_period = datetime.now(UTC).strftime("%Y-%m")
    usage_result = await session.execute(
        select(UsageCounter).where(
            UsageCounter.tenant_id == tenant_id,
            UsageCounter.period == current_period,
        )
    )
    usage = usage_result.scalar_one_or_none()

    if subscription_row is None:
        return BillingSettingsResponse(
            plan="trial",
            plan_name="Trial",
            subscription_status="trial",
            dialogs_used=usage.dialogs_count if usage else 0,
            dialogs_limit=0,
            ai_replies_used=usage.ai_replies_count if usage else 0,
            channel_limit=0,
        )

    subscription, plan = subscription_row
    return BillingSettingsResponse(
        plan=plan.code,
        plan_name=plan.name,
        subscription_status=subscription.status,
        dialogs_used=usage.dialogs_count if usage else 0,
        dialogs_limit=plan.dialog_limit,
        ai_replies_used=usage.ai_replies_count if usage else 0,
        channel_limit=plan.channel_limit,
    )


async def _get_or_create_ai_config(session: AsyncSession, tenant_id: UUID) -> TenantAIConfig:
    tenant = await _get_tenant(session, tenant_id)
    config = await session.get(TenantAIConfig, tenant.id)
    if config is None:
        config = TenantAIConfig(
            tenant_id=tenant.id,
            auto_reply_enabled=False,
            confidence_threshold=80,
            llm_provider=settings.LLM_PROVIDER,
            embedding_model=settings.EMBEDDING_MODEL,
            system_prompt="",
        )
        session.add(config)
        await session.commit()
        await session.refresh(config)
    return config


async def _get_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant not found")
    return tenant


def _ai_settings_response(config: TenantAIConfig) -> AISettingsResponse:
    return AISettingsResponse(
        auto_reply_enabled=config.auto_reply_enabled,
        confidence_threshold=config.confidence_threshold,
        llm_provider=config.llm_provider,
        embedding_model=config.embedding_model,
        system_prompt=config.system_prompt,
        available_providers=list(LLM_PROVIDERS),
    )


def _workspace_settings_response(tenant: Tenant) -> WorkspaceSettingsResponse:
    return WorkspaceSettingsResponse(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
    )
