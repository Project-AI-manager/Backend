"""Команда и профиль. Экраны: /settings/team, /profile."""
import uuid
from typing import cast

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.models.user import User
from app.schemas.auth import UserMeResponse

router = APIRouter()


@router.get("/me", response_model=UserMeResponse)
async def me(user: CurrentUser, session: SessionDep) -> UserMeResponse:
    db_user = cast(User | None, user.get("db_user"))
    if not db_user or db_user.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    return UserMeResponse(
        id=db_user.id,
        tenant_id=db_user.tenant_id,
        email=db_user.email,
        full_name=db_user.full_name,
        role=db_user.role,
        status=db_user.status,
        email_verified=db_user.email_verified_at is not None,
    )


@router.get("", response_model=list[UserMeResponse])
async def list_team(user: CurrentUser, session: SessionDep) -> list[UserMeResponse]:
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
        )
        for db_user in result.scalars().all()
    ]
