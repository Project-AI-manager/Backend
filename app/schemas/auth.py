"""Pydantic-схемы аутентификации."""
from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    company_name: str
    email: EmailStr
    password: str
    full_name: str = ""


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
