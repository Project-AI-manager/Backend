"""Tests for the no-Docker local demo bootstrap safety checks."""

import pytest

from app.core.config import settings
from app.db.local_demo import _ensure_sqlite_url


def test_local_demo_rejects_non_sqlite_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgresql+asyncpg://app:app@localhost:5432/ai_manager",
    )

    with pytest.raises(RuntimeError, match="only supports sqlite"):
        _ensure_sqlite_url()
