"""Bounded local storage and validation for conversation attachments."""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from app.core.config import settings

_MIME_TYPES = {
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
    ".pdf": {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True, slots=True)
class StoredConversationAttachment:
    metadata: dict[str, object]
    path: Path


def validate_and_store_attachment(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    filename: str,
    content_type: str | None,
    data: bytes,
) -> StoredConversationAttachment:
    safe_name = Path(filename or "").name.strip()
    extension = Path(safe_name).suffix.lower()
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    if (
        not safe_name
        or extension not in _MIME_TYPES
        or normalized_type not in _MIME_TYPES[extension]
    ):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Неподдерживаемый тип файла")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Файл пуст")
    if len(data) > settings.CONVERSATION_ATTACHMENT_MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Размер файла превышает 10 МБ")
    _validate_signature(extension, data)

    attachment_id = uuid4()
    storage_key = f"{tenant_id}/{conversation_id}/{attachment_id}{extension}"
    path = attachment_path(storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Не удалось сохранить файл"
        ) from exc

    kind = "image" if extension in _IMAGE_EXTENSIONS else "document"
    return StoredConversationAttachment(
        metadata={
            "id": str(attachment_id),
            "name": safe_name,
            "content_type": normalized_type,
            "size_bytes": len(data),
            "kind": kind,
            "storage_key": storage_key,
        },
        path=path,
    )


def attachment_path(storage_key: str) -> Path:
    root = Path(settings.CONVERSATION_UPLOAD_DIR).resolve()
    candidate = (root / storage_key).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Вложение не найдено")
    return candidate


def delete_attachment(path: Path) -> None:
    path.unlink(missing_ok=True)


def _validate_signature(extension: str, data: bytes) -> None:
    valid = False
    if extension in {".jpg", ".jpeg"}:
        valid = data.startswith(b"\xff\xd8\xff")
    elif extension == ".png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension == ".webp":
        valid = len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    elif extension == ".pdf":
        valid = data.startswith(b"%PDF-")
    elif extension in {".docx", ".xlsx"}:
        valid = _valid_ooxml(data, extension)
    else:
        try:
            data.decode("utf-8")
            valid = b"\x00" not in data
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Содержимое файла не соответствует его формату",
        )


def _valid_ooxml(data: bytes, extension: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    marker = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
    return "[Content_Types].xml" in names and marker in names
