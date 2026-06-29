"""Аутентификация: регистрация (создание компании), вход, refresh. Экран: /login, /register."""
from fastapi import APIRouter

from app.api.deps import SessionDep
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenPair
from app.services.auth import login_user, refresh_tokens, register_user

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
