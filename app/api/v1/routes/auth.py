"""Аутентификация: регистрация (создание компании), вход, refresh. Экран: /login, /register."""
from fastapi import APIRouter

from app.api.deps import SessionDep
from app.schemas.auth import (
    EmailActionResponse,
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    RequestPasswordResetRequest,
    ResetPasswordRequest,
    TokenPair,
)
from app.services.auth import login_user, logout_user, refresh_tokens, register_user
from app.services.email import request_password_reset, reset_password

router = APIRouter()


@router.post("/register", response_model=TokenPair)
async def register(body: RegisterRequest, session: SessionDep) -> TokenPair:
    return await register_user(session, body)


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, session: SessionDep) -> TokenPair:
    return await login_user(session, body)


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, session: SessionDep) -> TokenPair:
    return await refresh_tokens(session, body)


@router.post("/logout", response_model=LogoutResponse)
async def logout(body: LogoutRequest, session: SessionDep) -> LogoutResponse:
    await logout_user(session, body)
    return LogoutResponse()


@router.post("/password-reset/request", response_model=EmailActionResponse)
async def request_password_reset_email(
    body: RequestPasswordResetRequest,
    session: SessionDep,
) -> EmailActionResponse:
    return await request_password_reset(session, str(body.email))


@router.post("/password-reset/confirm", response_model=EmailActionResponse)
async def confirm_password_reset(
    body: ResetPasswordRequest,
    session: SessionDep,
) -> EmailActionResponse:
    return await reset_password(session, body.token, body.new_password)
