"""Transactional email service with local dev outbox and optional SMTP delivery."""

from __future__ import annotations

import asyncio
import secrets
import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password, hash_token
from app.models.email import EmailOutbox, EmailToken
from app.models.user import User
from app.schemas.auth import EmailActionResponse
from app.schemas.email import EmailOutboxResponse, EmailStatusResponse

VERIFY_EMAIL = "verify_email"
PASSWORD_RESET = "password_reset"


def email_status() -> EmailStatusResponse:
    return EmailStatusResponse(
        send_enabled=settings.EMAIL_SEND_ENABLED,
        dev_mode=settings.EMAIL_DEV_MODE,
        smtp_configured=bool(settings.SMTP_HOST),
        from_email=settings.EMAIL_FROM,
    )


async def request_email_verification(
    session: AsyncSession,
    user: User,
) -> EmailActionResponse:
    if user.email_verified_at is not None:
        return EmailActionResponse(ok=True, sent=False, dev_token=None)

    token = await _create_token(session, user, VERIFY_EMAIL)
    sent = await _queue_and_maybe_send(
        session,
        user=user,
        purpose=VERIFY_EMAIL,
        subject="Подтвердите почту в Автопилоте",
        body_text=(
            "Здравствуйте!\n\n"
            "Чтобы подтвердить почту в Автопилоте, используйте одноразовый код:\n\n"
            f"{token}\n\n"
            "Если вы не создавали аккаунт, просто проигнорируйте это письмо."
        ),
        metadata={"token_hint": token[-6:]},
    )
    await session.commit()
    return EmailActionResponse(
        ok=True,
        sent=sent,
        dev_token=token if settings.EMAIL_DEV_MODE else None,
    )


async def verify_email(session: AsyncSession, token: str) -> EmailActionResponse:
    email_token = await _consume_token(session, token, VERIFY_EMAIL)
    user = await session.get(User, email_token.user_id)
    if not user or user.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    user.email_verified_at = datetime.now(UTC)
    await session.commit()
    return EmailActionResponse(ok=True, sent=False, dev_token=None)


async def request_password_reset(
    session: AsyncSession,
    email: str,
) -> EmailActionResponse:
    normalized = email.strip().lower()
    result = await session.execute(select(User).where(User.email == normalized))
    user = result.scalar_one_or_none()

    # Не раскрываем, существует ли адрес.
    if not user or user.status != "active":
        return EmailActionResponse(ok=True, sent=False, dev_token=None)

    token = await _create_token(session, user, PASSWORD_RESET)
    sent = await _queue_and_maybe_send(
        session,
        user=user,
        purpose=PASSWORD_RESET,
        subject="Сброс пароля в Автопилоте",
        body_text=(
            "Здравствуйте!\n\n"
            "Для сброса пароля используйте одноразовый код:\n\n"
            f"{token}\n\n"
            "Код действует ограниченное время. Если вы не запрашивали сброс, "
            "просто проигнорируйте письмо."
        ),
        metadata={"token_hint": token[-6:]},
    )
    await session.commit()
    return EmailActionResponse(
        ok=True,
        sent=sent,
        dev_token=token if settings.EMAIL_DEV_MODE else None,
    )


async def reset_password(
    session: AsyncSession,
    token: str,
    new_password: str,
) -> EmailActionResponse:
    email_token = await _consume_token(session, token, PASSWORD_RESET)
    user = await session.get(User, email_token.user_id)
    if not user or user.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    user.password_hash = hash_password(new_password)
    await session.commit()
    return EmailActionResponse(ok=True, sent=False, dev_token=None)


async def list_outbox(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    limit: int = 50,
) -> list[EmailOutboxResponse]:
    result = await session.execute(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_id)
        .order_by(desc(EmailOutbox.created_at))
        .limit(limit)
    )
    return [_outbox_response(item) for item in result.scalars().all()]


async def _create_token(session: AsyncSession, user: User, purpose: str) -> str:
    raw = secrets.token_urlsafe(32)
    session.add(
        EmailToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(raw),
            email=user.email,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.EMAIL_TOKEN_TTL_MIN),
            used_at=None,
        )
    )
    await session.flush()
    return raw


async def _consume_token(session: AsyncSession, raw_token: str, purpose: str) -> EmailToken:
    result = await session.execute(
        select(EmailToken).where(
            EmailToken.token_hash == hash_token(raw_token),
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
        )
    )
    email_token = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if not email_token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired email token")

    expires_at = email_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired email token")

    email_token.used_at = now
    return email_token


async def _queue_and_maybe_send(
    session: AsyncSession,
    *,
    user: User,
    purpose: str,
    subject: str,
    body_text: str,
    metadata: dict,
) -> bool:
    outbox = EmailOutbox(
        tenant_id=user.tenant_id,
        user_id=user.id,
        to_email=user.email,
        subject=subject,
        body_text=body_text,
        purpose=purpose,
        status="queued",
        metadata_json=metadata,
    )
    session.add(outbox)
    await session.flush()

    if not settings.EMAIL_SEND_ENABLED:
        outbox.status = "dev"
        return False

    if not settings.SMTP_HOST:
        outbox.status = "failed"
        outbox.error = "SMTP_HOST is not configured"
        return False

    try:
        await asyncio.to_thread(_send_smtp, outbox)
    except (OSError, smtplib.SMTPException) as exc:
        outbox.status = "failed"
        outbox.error = str(exc)
        return False

    outbox.status = "sent"
    return True


def _send_smtp(outbox: EmailOutbox) -> None:
    message = EmailMessage()
    message["From"] = settings.EMAIL_FROM
    message["To"] = outbox.to_email
    message["Subject"] = outbox.subject
    message.set_content(outbox.body_text)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


def _outbox_response(item: EmailOutbox) -> EmailOutboxResponse:
    return EmailOutboxResponse(
        id=item.id,
        to_email=item.to_email,
        subject=item.subject,
        purpose=item.purpose,
        status=item.status,
        error=item.error,
        created_at=item.created_at,
    )
