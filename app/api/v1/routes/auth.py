"""Аутентификация: регистрация (создание компании), вход, refresh. Экран: /login, /register."""
from fastapi import APIRouter

from app.schemas.auth import LoginRequest, RegisterRequest, TokenPair

router = APIRouter()


@router.post("/register", response_model=TokenPair)
async def register(body: RegisterRequest) -> TokenPair:
    raise NotImplementedError  # TODO: создать Tenant + User(owner), вернуть пару токенов


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest) -> TokenPair:
    raise NotImplementedError  # TODO: проверить пароль (argon2), выдать access+refresh


@router.post("/refresh", response_model=TokenPair)
async def refresh(refresh_token: str) -> TokenPair:
    raise NotImplementedError  # TODO: ротация refresh-токена
