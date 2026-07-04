"""Email module schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr


class EmailStatusResponse(BaseModel):
    send_enabled: bool
    dev_mode: bool
    smtp_configured: bool
    from_email: str


class EmailOutboxResponse(BaseModel):
    id: UUID
    to_email: EmailStr
    subject: str
    purpose: str
    status: str
    error: str
    created_at: datetime
