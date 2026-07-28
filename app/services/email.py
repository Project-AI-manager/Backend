"""Transactional email service with local dev outbox and optional SMTP delivery."""

from __future__ import annotations

import asyncio
import secrets
import smtplib
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid

from fastapi import HTTPException, status
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password, hash_token
from app.models.email import EmailOutbox, EmailToken
from app.models.user import User, UserNotificationSettings
from app.schemas.auth import EmailActionResponse
from app.schemas.email import EmailOutboxResponse, EmailStatusResponse
from app.services.email_assets import (
    AUTOPILOT_LOGO_CID,
    AUTOPILOT_LOGO_FILENAME,
    AUTOPILOT_LOGO_PNG,
)
from app.services.email_templates import (
    escalation_email,
    password_reset_email,
    verification_email,
)

VERIFY_EMAIL = "verify_email"
PASSWORD_RESET = "password_reset"
ESCALATION_ALERT = "escalation_alert"


def email_status() -> EmailStatusResponse:
    return EmailStatusResponse(
        send_enabled=settings.EMAIL_SEND_ENABLED,
        dev_mode=settings.EMAIL_DEV_MODE,
        smtp_configured=bool(
            settings.SMTP_HOST
            and settings.SMTP_USERNAME
            and settings.SMTP_PASSWORD
        ),
        from_email=settings.EMAIL_FROM,
    )
async def request_email_verification(
    session: AsyncSession,
    user: User,
) -> EmailActionResponse:
    if user.email_verified_at is not None:
        return EmailActionResponse(ok=True, sent=False, dev_token=None)

    token = await _create_token(session, user, VERIFY_EMAIL)
    body_text, body_html = verification_email(
        name=user.full_name,
        code=token,
        ttl_minutes=settings.EMAIL_TOKEN_TTL_MIN,
    )
    sent = await _queue_and_maybe_send(
        session,
        user=user,
        purpose=VERIFY_EMAIL,
        subject="Подтвердите почту в Автопилоте",
        body_text=body_text,
        body_html=body_html,
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")

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
    body_text, body_html = password_reset_email(
        code=token,
        ttl_minutes=settings.EMAIL_TOKEN_TTL_MIN,
    )
    sent = await _queue_and_maybe_send(
        session,
        user=user,
        purpose=PASSWORD_RESET,
        subject="Сброс пароля в Автопилоте",
        body_text=body_text,
        body_html=body_html,
        metadata={"token_hint": token[-6:]},
    )
    await session.commit()
    return EmailActionResponse(
        ok=True,
        sent=sent,
        dev_token=token if settings.EMAIL_DEV_MODE else None,
    )


async def send_escalation_alerts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_name: str,
    message_preview: str,
    conversation_id: uuid.UUID,
) -> int:
    """Notify active team members who opted into escalation emails."""
    result = await session.execute(
        select(User)
        .outerjoin(UserNotificationSettings, UserNotificationSettings.user_id == User.id)
        .where(
            User.tenant_id == tenant_id,
            User.status == "active",
            or_(
                UserNotificationSettings.user_id.is_(None),
                UserNotificationSettings.escalation_email_enabled.is_(True),
            ),
        )
    )
    recipients = result.scalars().all()
    conversation_url = f"{settings.app_public_href}/inbox?conversation={conversation_id}"
    body_text, body_html = escalation_email(
        customer_name=customer_name,
        message_preview=message_preview,
        conversation_url=conversation_url,
    )
    for recipient in recipients:
        await _queue_and_maybe_send(
            session,
            user=recipient,
            purpose=ESCALATION_ALERT,
            subject="В диалоге нужен человек",
            body_text=body_text,
            body_html=body_html,
            metadata={
                "customer_name": customer_name,
                "conversation_id": str(conversation_id),
                "conversation_url": conversation_url,
                "conversation_display_url": (
                    f"{settings.app_public_display_url}/inbox?conversation={conversation_id}"
                ),
            },
        )
    return len(recipients)


async def reset_password(
    session: AsyncSession,
    token: str,
    new_password: str,
) -> EmailActionResponse:
    email_token = await _consume_token(session, token, PASSWORD_RESET)
    user = await session.get(User, email_token.user_id)
    if not user or user.status != "active":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")

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
    raw = f"{secrets.randbelow(1_000_000):06d}"
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
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Неверный или просроченный код подтверждения",
        )

    expires_at = email_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Неверный или просроченный код подтверждения",
        )

    email_token.used_at = now
    return email_token


async def _queue_and_maybe_send(
    session: AsyncSession,
    *,
    user: User,
    purpose: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
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
        metadata_json={**metadata, **({"body_html": body_html} if body_html else {})},
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
    message = EmailMessage(policy=SMTP)
    message["From"] = settings.EMAIL_FROM
    message["To"] = outbox.to_email
    message["Subject"] = outbox.subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=_sender_domain())
    message.set_content(outbox.body_text, subtype="plain", charset="utf-8")
    body_html = (outbox.metadata_json or {}).get("body_html")
    if isinstance(body_html, str) and body_html:
        message.add_alternative(
            _html_with_embedded_logo_fallback(body_html),
            subtype="html",
            charset="utf-8",
        )
        html_part = message.get_payload()[-1]
        # This creates the broadly supported MIME tree:
        # multipart/alternative(text/plain, multipart/related(text/html, image/png)).
        # Mail clients can resolve either the CID URL or the Content-Location URL.
        html_part.add_related(
            AUTOPILOT_LOGO_PNG,
            maintype="image",
            subtype="png",
            cid=f"<{AUTOPILOT_LOGO_CID}>",
            filename=AUTOPILOT_LOGO_FILENAME,
            disposition="inline",
            params={"name": AUTOPILOT_LOGO_FILENAME},
            headers=(
                f"Content-Location: {AUTOPILOT_LOGO_FILENAME}",
                f"X-Attachment-Id: {AUTOPILOT_LOGO_CID}",
            ),
        )

    smtp_factory = smtplib.SMTP_SSL if settings.SMTP_USE_SSL else smtplib.SMTP
    connection_kwargs = {"timeout": 10}
    if settings.SMTP_USE_SSL:
        connection_kwargs["context"] = ssl.create_default_context()
    with smtp_factory(settings.SMTP_HOST, settings.SMTP_PORT, **connection_kwargs) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(message)


def _html_with_embedded_logo_fallback(body_html: str) -> str:
    """Let clients that ignore CID find the same related part by location."""
    return body_html.replace(
        f'src="cid:{AUTOPILOT_LOGO_CID}"',
        (
            f'src="cid:{AUTOPILOT_LOGO_CID}" '
            f'data-fallback-src="{AUTOPILOT_LOGO_FILENAME}"'
        ),
    )


def _sender_domain() -> str:
    sender = settings.EMAIL_FROM.rpartition("@")[2].rstrip(">").strip()
    return sender or "localhost"


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
