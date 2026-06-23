"""Команда и профиль. Экраны: /settings/team, /profile."""
from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter()


@router.get("/me")
async def me(user: CurrentUser) -> dict:
    return user  # TODO: вернуть профиль из БД


@router.get("")
async def list_team(user: CurrentUser) -> list[dict]:
    return []  # TODO: список сотрудников тенанта
