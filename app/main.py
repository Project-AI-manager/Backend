"""Сборка FastAPI-приложения: middleware, роутеры, healthcheck.

Поток обработки обращения и слои — см. wiki/concepts/system-architecture.md.
"""
import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import configure_exception_handlers
from app.core.logging import configure_logging
from app.services.rag.vector_store import close_vector_stores
from app.workers.telegram_listener import run_listener_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    # Re-run safeguards here so mutated or dynamically supplied settings fail before serving.
    settings.assert_safe_runtime()
    configure_logging()
    telegram_listener_task: asyncio.Task[None] | None = None
    if settings.TELEGRAM_LISTENER_IN_PROCESS:
        telegram_listener_task = asyncio.create_task(
            run_listener_loop(),
            name="telegram-listener-in-process",
        )
    # TODO: прогреть подключения (Qdrant, Redis) при старте
    try:
        yield
    finally:
        if telegram_listener_task is not None:
            telegram_listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await telegram_listener_task
        await close_vector_stores()


app = FastAPI(title="AI-сотрудник в едином окне", version="0.1.0", lifespan=lifespan)
configure_exception_handlers(app)

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
