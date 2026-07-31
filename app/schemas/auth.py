"""Pydantic-схемы аутентификации."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    company_name: str
    email: EmailStr
    password: str
    full_name: str = ""


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class LogoutResponse(BaseModel):
    revoked: bool = True


class RequestPasswordResetRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class VerifyEmailRequest(BaseModel):
    token: str


class EmailActionResponse(BaseModel):
    ok: bool = True
    sent: bool = False
    dev_token: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserMeResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    email: EmailStr
    full_name: str
    role: str
    status: str
    email_verified: bool = False
    onboarding_seen: bool = False


class OnboardingStatusResponse(BaseModel):
    onboarding_seen: bool


class NotificationSettingsResponse(BaseModel):
    escalation_email_enabled: bool
    daily_digest_email_enabled: bool


class NotificationSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    escalation_email_enabled: bool | None = None
    daily_digest_email_enabled: bool | None = None
