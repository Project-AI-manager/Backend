"""Общие зависимости: сессия БД, текущий пользователь (JWT), скоуп тенанта."""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_session
from app.models.user import User

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Декодирует access-JWT и проверяет, что пользователь ещё активен в БД."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token type")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
        token_tenant_id = uuid.UUID(str(payload["tenant_id"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token subject") from exc

    db_user = await session.get(User, user_id)
    if not db_user or db_user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User is not active")
    if db_user.tenant_id != token_tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid tenant scope")

    payload["db_user"] = db_user
    payload["tenant_id"] = str(db_user.tenant_id)
    payload["role"] = db_user.role
    return payload


CurrentUser = Annotated[dict, Depends(get_current_user)]


def tenant_id_from_user(user: dict) -> uuid.UUID:
    """Return the trusted tenant scope stored in the access token."""
    raw_tenant_id = user.get("tenant_id")
    if not raw_tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Tenant is required")
    try:
        return uuid.UUID(str(raw_tenant_id))
    except ValueError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid tenant id") from exc
