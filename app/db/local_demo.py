"""Local no-Docker demo bootstrap.

This module is intentionally SQLite-only. The production/dev-infra path still
uses Alembic + PostgreSQL; this is a quick local sandbox for manual testing on
machines where Docker/Postgres are unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

import app.models  # noqa: F401 - register all SQLAlchemy models in Base.metadata
from app.core.config import settings
from app.db.base import Base
from app.db.seed import DEMO_CREDENTIALS, seed_demo_data
from app.db.session import SessionLocal, engine


@dataclass(slots=True)
class LocalDemoResult:
    created: int
    updated: int
    reset: bool


def _ensure_sqlite_url() -> None:
    if not settings.DATABASE_URL.startswith("sqlite+aiosqlite:"):
        raise RuntimeError(
            "Local demo bootstrap only supports sqlite+aiosqlite DATABASE_URL. "
            "Use Alembic migrations for PostgreSQL."
        )


async def bootstrap_local_demo(*, reset: bool = False) -> LocalDemoResult:
    _ensure_sqlite_url()

    async with engine.begin() as connection:
        if reset:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        stats = await seed_demo_data(session)

    return LocalDemoResult(created=stats.created, updated=stats.updated, reset=reset)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap a SQLite local demo database.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate all SQLite tables before seeding demo data.",
    )
    args = parser.parse_args()

    result = asyncio.run(bootstrap_local_demo(reset=args.reset))
    print(
        "Local demo database ready: "
        f"created={result.created}, updated={result.updated}, reset={result.reset}"
    )
    print(f"Demo login: {DEMO_CREDENTIALS['owner_email']} / {DEMO_CREDENTIALS['password']}")


if __name__ == "__main__":
    main()
