"""Transactional email module: verification, password reset and outbox."""

from typing import cast

from fastapi import APIRouter

from app.api.deps import AdminUser, CurrentUser, SessionDep, tenant_id_from_user
from app.models.user import User
from app.schemas.auth import (
    EmailActionResponse,
    RequestPasswordResetRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from app.schemas.email import EmailDeliverabilityResponse, EmailOutboxResponse, EmailStatusResponse
from app.services.email import (
    email_deliverability,
    email_status,
    list_outbox,
    request_email_verification,
    request_password_reset,
    reset_password,
    verify_email,
)

router = APIRouter()


@router.get("/status", response_model=EmailStatusResponse)
async def status() -> EmailStatusResponse:
    return email_status()


@router.get("/deliverability", response_model=EmailDeliverabilityResponse)
async def deliverability(user: AdminUser) -> EmailDeliverabilityResponse:
    del user
    return email_deliverability()


@router.post("/verification/request", response_model=EmailActionResponse)
async def request_verification(
    user: CurrentUser,
    session: SessionDep,
) -> EmailActionResponse:
    db_user = cast(User, user["db_user"])
    return await request_email_verification(session, db_user)


@router.post("/verification/confirm", response_model=EmailActionResponse)
async def confirm_verification(
    body: VerifyEmailRequest,
    session: SessionDep,
) -> EmailActionResponse:
    return await verify_email(session, body.token)


@router.post("/password-reset/request", response_model=EmailActionResponse)
async def request_reset(
    body: RequestPasswordResetRequest,
    session: SessionDep,
) -> EmailActionResponse:
    return await request_password_reset(session, str(body.email))


@router.post("/password-reset/confirm", response_model=EmailActionResponse)
async def confirm_reset(
    body: ResetPasswordRequest,
    session: SessionDep,
) -> EmailActionResponse:
    return await reset_password(session, body.token, body.new_password)


@router.get("/outbox", response_model=list[EmailOutboxResponse])
async def outbox(
    user: AdminUser,
    session: SessionDep,
) -> list[EmailOutboxResponse]:
    return await list_outbox(session, tenant_id_from_user(user))
