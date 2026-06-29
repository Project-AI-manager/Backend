"""Общие зависимости: сессия БД, текущий пользователь (JWT), скоуп тенанта."""

import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(authorization: Annotated[str | None, Header()] = None) -> dict:
    """Декодирует access-JWT из заголовка Authorization. TODO: подгружать User из БД и tenant_id."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    if payload.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token type")
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
