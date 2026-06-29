"""Auth service: registration, login and refresh-token rotation."""
import re
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    hash_token,
    refresh_token_expires_at,
    verify_password,
)
from app.models.tenant import Tenant, TenantAIConfig
from app.models.user import RefreshToken, User
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenPair


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "company"


def _token_pair_for_user(user: User) -> TokenPair:
    access = create_token(user.id, tenant_id=user.tenant_id, role=user.role)
    refresh = create_token(user.id, tenant_id=user.tenant_id, role=user.role, refresh=True)
    return TokenPair(access_token=access, refresh_token=refresh)


async def _store_refresh_token(session: AsyncSession, user: User, refresh_token: str) -> None:
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=refresh_token_expires_at(),
        )
    )


async def _get_active_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == _normalize_email(email)))
    user = result.scalar_one_or_none()
    if not user or user.status != "active":
        return None
    return user


async def register_user(session: AsyncSession, body: RegisterRequest) -> TokenPair:
    email = _normalize_email(str(body.email))
    existing = await session.execute(select(User.id).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User with this email already exists",
        )

    tenant = Tenant(
        name=body.company_name.strip(),
        slug=f"{_slugify(body.company_name)}-{secrets.token_hex(3)}",
        status="active",
    )
    session.add(tenant)
    await session.flush()

    session.add(
        TenantAIConfig(
            tenant_id=tenant.id,
            auto_reply_enabled=False,
            confidence_threshold=80,
            llm_provider="mock",
            embedding_model="multilingual-e5-large",
            system_prompt="",
        )
    )

    user = User(
        tenant_id=tenant.id,
        email=email,
        full_name=body.full_name.strip(),
        role="owner",
        password_hash=hash_password(body.password),
        status="active",
    )
    session.add(user)
    await session.flush()

    tokens = _token_pair_for_user(user)
    await _store_refresh_token(session, user, tokens.refresh_token)
    await session.commit()
    return tokens


async def login_user(session: AsyncSession, body: LoginRequest) -> TokenPair:
    user = await _get_active_user_by_email(session, str(body.email))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    tokens = _token_pair_for_user(user)
    await _store_refresh_token(session, user, tokens.refresh_token)
    await session.commit()
    return tokens


async def refresh_tokens(session: AsyncSession, body: RefreshRequest) -> TokenPair:
    try:
        payload = decode_token(body.refresh_token)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from exc

    if payload.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token type")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token subject") from exc

    token_hash = hash_token(body.refresh_token)
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
    )
    stored = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if not stored:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired or revoked")
    expires_at = stored.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired or revoked")

    user = await session.get(User, user_id)
    if not user or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User is not active")

    stored.revoked_at = now
    tokens = _token_pair_for_user(user)
    await _store_refresh_token(session, user, tokens.refresh_token)
    await session.commit()
    return tokens
