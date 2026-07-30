"""Private tenant-scoped storage for customer profile images."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from app.core.config import settings


def store_customer_avatar(tenant_id: UUID, customer_id: UUID, data: bytes) -> bool:
    """Atomically store a Telegram avatar after validating its image signature."""
    detected = _detect_image(data)
    if detected is None or len(data) > settings.CUSTOMER_AVATAR_MAX_BYTES:
        return False
    extension, _content_type = detected
    path = _avatar_path(tenant_id, customer_id, extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    remove_customer_avatar(tenant_id, customer_id, except_path=path)
    return True


def remove_customer_avatar(
    tenant_id: UUID,
    customer_id: UUID,
    *,
    except_path: Path | None = None,
) -> None:
    for extension in (".jpg", ".png", ".webp"):
        candidate = _avatar_path(tenant_id, customer_id, extension)
        if except_path is None or candidate != except_path:
            candidate.unlink(missing_ok=True)


def get_customer_avatar(tenant_id: UUID, customer_id: UUID) -> tuple[Path, str] | None:
    for extension, content_type in (
        (".jpg", "image/jpeg"),
        (".png", "image/png"),
        (".webp", "image/webp"),
    ):
        path = _avatar_path(tenant_id, customer_id, extension)
        if path.is_file():
            return path, content_type
    return None


def customer_avatar_exists(tenant_id: UUID, customer_id: UUID) -> bool:
    return get_customer_avatar(tenant_id, customer_id) is not None


def _avatar_path(tenant_id: UUID, customer_id: UUID, extension: str) -> Path:
    root = Path(settings.CUSTOMER_AVATAR_DIR).resolve()
    candidate = (root / str(tenant_id) / f"{customer_id}{extension}").resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Invalid customer avatar path")
    return candidate


def _detect_image(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None
