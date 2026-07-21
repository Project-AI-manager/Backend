"""Rebuild Qdrant knowledge vectors after an embedding model or dimension change."""

from __future__ import annotations

import argparse
import asyncio
import uuid

from app.db.session import SessionLocal
from app.services.knowledge import reindex_ready_documents


async def _run(tenant_id: uuid.UUID | None) -> tuple[int, int]:
    async with SessionLocal() as session:
        return await reindex_ready_documents(session, tenant_id=tenant_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reindex ready knowledge documents into the configured Qdrant collection."
    )
    parser.add_argument("--tenant-id", type=uuid.UUID, default=None)
    args = parser.parse_args()
    documents, chunks = asyncio.run(_run(args.tenant_id))
    print(f"Reindexed {documents} documents and {chunks} chunks")


if __name__ == "__main__":
    main()
