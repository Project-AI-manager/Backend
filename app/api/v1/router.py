"""Агрегация роутеров API v1."""
from fastapi import APIRouter

from app.api.v1.routes import (
    analytics,
    auth,
    channels,
    conversations,
    knowledge,
    ml,
    settings,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])
api_router.include_router(ml.router, prefix="/ml", tags=["ml"])
api_router.include_router(channels.router, prefix="/channels", tags=["channels"])
api_router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
