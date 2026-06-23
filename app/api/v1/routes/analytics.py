"""Аналитика: KPI дашборда. Экран: /analytics."""
from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter()


@router.get("/overview")
async def overview(user: CurrentUser) -> dict:
    # TODO: доля автоответов, среднее время ответа, диалогов/лимит, рост базы
    return {"auto_reply_rate": 0, "avg_response_sec": 0, "dialogs_used": 0, "dialogs_limit": 0}
