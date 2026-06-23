"""Настройки тенанта: AI, тариф, компания. Экраны: /settings/*."""
from fastapi import APIRouter

from app.api.deps import CurrentUser

router = APIRouter()


@router.get("/ai")
async def get_ai_settings(user: CurrentUser) -> dict:
    # TODO: порог уверенности, авто-ответ, рабочие часы, system prompt
    return {"auto_reply_enabled": False, "confidence_threshold": 80}


@router.put("/ai")
async def update_ai_settings(user: CurrentUser) -> dict:
    raise NotImplementedError


@router.get("/billing")
async def get_billing(user: CurrentUser) -> dict:
    # TODO: текущий план, использование диалогов
    return {"plan": "trial", "dialogs_used": 0, "dialogs_limit": 0}
