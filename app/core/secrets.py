"""Helpers for storing channel credentials outside API responses."""

from base64 import urlsafe_b64encode
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

SECRET_PREFIX = "fernet:"


def encrypt_secret(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return f"{SECRET_PREFIX}{token}"


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(SECRET_PREFIX):
        return stored
    token = stored.removeprefix(SECRET_PREFIX).encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Stored secret cannot be decrypted") from exc


def _fernet() -> Fernet:
    digest = sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(urlsafe_b64encode(digest))
