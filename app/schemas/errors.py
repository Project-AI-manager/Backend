"""Stable API error response models shared by every endpoint."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ValidationIssue(BaseModel):
    location: list[str | int] = Field(default_factory=list)
    message: str
    type: str


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    msg: str
    errors: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    detail: ErrorDetail


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Authentication required"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Resource not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Request validation failed"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
    502: {"model": ErrorResponse, "description": "Upstream service error"},
    503: {"model": ErrorResponse, "description": "Service unavailable"},
}
