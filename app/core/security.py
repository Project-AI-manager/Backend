"""JWT (access/refresh), хеширование паролей и refresh-token lookup."""
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher

from app.core.config import settings

_ph = PasswordHasher()
ALGORITHM = "HS256"


def hash_password(raw: str) -> str:
    return _ph.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, raw)
    except Exception:
        return False


def create_token(
    subject: str,
    *,
    tenant_id: str | uuid.UUID | None = None,
    role: str | None = None,
    refresh: bool = False,
) -> str:
    now = datetime.now(UTC)
    ttl = (
        timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
        if refresh
        else timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN)
    )
    payload = {
        "sub": str(subject),
        "type": "refresh" if refresh else "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + ttl,
    }
    if tenant_id is not None:
        payload["tenant_id"] = str(tenant_id)
    if role is not None:
        payload["role"] = role
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
