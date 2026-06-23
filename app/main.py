"""Сборка FastAPI-приложения: middleware, роутеры, healthcheck.

Поток обработки обращения и слои — см. wiki/concepts/system-architecture.md.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # TODO: прогреть подключения (Qdrant, Redis) при старте
    yield
    # TODO: graceful shutdown


app = FastAPI(title="AI-сотрудник в едином окне", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
