"""JWT (access/refresh) и хеширование паролей (argon2). См. backend-stack-explained."""
from datetime import datetime, timedelta, timezone

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


def create_token(subject: str, *, refresh: bool = False) -> str:
    now = datetime.now(timezone.utc)
    ttl = (
        timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
        if refresh
        else timedelta(minutes=settings.ACCESS_TOKEN_TTL_MIN)
    )
    payload = {"sub": subject, "type": "refresh" if refresh else "access", "iat": now, "exp": now + ttl}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
