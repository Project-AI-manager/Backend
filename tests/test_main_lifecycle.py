"""Application lifespan tests for optional in-process Telegram ingestion."""

import asyncio

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app import main as main_module
from app.core.config import Settings


def test_telegram_listener_in_process_setting_parses_boolean() -> None:
    assert Settings(TELEGRAM_LISTENER_IN_PROCESS="true").TELEGRAM_LISTENER_IN_PROCESS is True

    with pytest.raises(ValidationError):
        Settings(TELEGRAM_LISTENER_IN_PROCESS="not-a-boolean")


def test_in_process_listener_is_rejected_outside_local_test() -> None:
    with pytest.raises(ValidationError, match="only supported in local/test"):
        Settings(
            APP_ENV="production",
            SECRET_KEY="a-production-secret-key-that-is-long-enough",
            DATABASE_URL="postgresql+asyncpg://app:app@localhost/ai_manager",
            EMAIL_DEV_MODE=False,
            APP_PUBLIC_URL="https://example.com",
            TELEGRAM_LISTENER_IN_PROCESS=True,
        )


@pytest.mark.asyncio
async def test_lifespan_does_not_start_listener_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener_called = False
    vector_stores_closed = False

    async def fake_listener() -> None:
        nonlocal listener_called
        listener_called = True

    async def fake_close_vector_stores() -> None:
        nonlocal vector_stores_closed
        vector_stores_closed = True

    monkeypatch.setattr(main_module.settings, "TELEGRAM_LISTENER_IN_PROCESS", False)
    monkeypatch.setattr(main_module, "run_listener_loop", fake_listener)
    monkeypatch.setattr(main_module, "close_vector_stores", fake_close_vector_stores)

    async with main_module.lifespan(FastAPI()):
        await asyncio.sleep(0)

    assert listener_called is False
    assert vector_stores_closed is True


@pytest.mark.asyncio
async def test_lifespan_stops_listener_before_closing_shared_vector_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener_started = asyncio.Event()
    events: list[str] = []

    async def fake_listener() -> None:
        events.append("listener-started")
        listener_started.set()
        try:
            await asyncio.Future()
        finally:
            events.append("listener-stopped")

    async def fake_close_vector_stores() -> None:
        events.append("vector-store-closed")

    monkeypatch.setattr(main_module.settings, "TELEGRAM_LISTENER_IN_PROCESS", True)
    monkeypatch.setattr(main_module, "run_listener_loop", fake_listener)
    monkeypatch.setattr(main_module, "close_vector_stores", fake_close_vector_stores)

    async with main_module.lifespan(FastAPI()):
        await asyncio.wait_for(listener_started.wait(), timeout=1)

    assert events == [
        "listener-started",
        "listener-stopped",
        "vector-store-closed",
    ]
