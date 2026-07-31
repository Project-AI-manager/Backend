"""Команда и профиль. Экраны: /settings/team, /profile."""

import uuid
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.models.user import User, UserNotificationSettings
from app.schemas.auth import (
    NotificationSettingsResponse,
    NotificationSettingsUpdate,
    OnboardingStatusResponse,
    UserMeResponse,
)

router = APIRouter()


@router.get("/me/notifications", response_model=NotificationSettingsResponse)
async def get_notification_settings(
    user: CurrentUser,
    session: SessionDep,
) -> NotificationSettingsResponse:
    db_user = _active_db_user(user)
    preferences = await _get_or_create_notification_settings(session, db_user.id)
    return _notification_settings_response(preferences)


@router.put("/me/notifications", response_model=NotificationSettingsResponse)
async def update_notification_settings(
    body: NotificationSettingsUpdate,
    user: CurrentUser,
    session: SessionDep,
) -> NotificationSettingsResponse:
    db_user = _active_db_user(user)
    preferences = await _get_or_create_notification_settings(session, db_user.id)
    if body.escalation_email_enabled is not None:
        preferences.escalation_email_enabled = body.escalation_email_enabled
    if body.daily_digest_email_enabled is not None:
        preferences.daily_digest_email_enabled = body.daily_digest_email_enabled
    await session.commit()
    await session.refresh(preferences)
    return _notification_settings_response(preferences)


@router.get("/me", response_model=UserMeResponse)
async def me(user: CurrentUser, session: SessionDep) -> UserMeResponse:
    db_user = _active_db_user(user)

    return UserMeResponse(
        id=db_user.id,
        tenant_id=db_user.tenant_id,
        email=db_user.email,
        full_name=db_user.full_name,
        role=db_user.role,
        status=db_user.status,
        email_verified=db_user.email_verified_at is not None,
        onboarding_seen=db_user.onboarding_seen_at is not None,
    )


@router.post("/me/onboarding/seen", response_model=OnboardingStatusResponse)
async def mark_onboarding_seen(
    user: CurrentUser,
    session: SessionDep,
) -> OnboardingStatusResponse:
    db_user = _active_db_user(user)
    if db_user.onboarding_seen_at is None:
        db_user.onboarding_seen_at = datetime.now(UTC)
        await session.commit()
    return OnboardingStatusResponse(onboarding_seen=True)


@router.get("", response_model=list[UserMeResponse])
async def list_team(user: AdminUser, session: SessionDep) -> list[UserMeResponse]:
    tenant_id = user.get("tenant_id")
    if not tenant_id:
        return []

    result = await session.execute(
        select(User).where(User.tenant_id == uuid.UUID(str(tenant_id)), User.status == "active")
    )
    return [
        UserMeResponse(
            id=db_user.id,
            tenant_id=db_user.tenant_id,
            email=db_user.email,
            full_name=db_user.full_name,
            role=db_user.role,
            status=db_user.status,
            email_verified=db_user.email_verified_at is not None,
            onboarding_seen=db_user.onboarding_seen_at is not None,
        )
        for db_user in result.scalars().all()
    ]


def _active_db_user(user: CurrentUser) -> User:
    db_user = cast(User | None, user.get("db_user"))
    if not db_user or db_user.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    return db_user


async def _get_or_create_notification_settings(
    session: SessionDep,
    user_id: uuid.UUID,
) -> UserNotificationSettings:
    preferences = await session.get(UserNotificationSettings, user_id)
    if preferences is None:
        preferences = UserNotificationSettings(
            user_id=user_id,
            escalation_email_enabled=True,
            daily_digest_email_enabled=False,
        )
        session.add(preferences)
        await session.commit()
        await session.refresh(preferences)
    return preferences


def _notification_settings_response(
    preferences: UserNotificationSettings,
) -> NotificationSettingsResponse:
    return NotificationSettingsResponse(
        escalation_email_enabled=preferences.escalation_email_enabled,
        daily_digest_email_enabled=preferences.daily_digest_email_enabled,
    )
