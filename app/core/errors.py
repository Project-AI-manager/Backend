"""Application-wide exception handlers for the stable API error contract."""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.schemas.errors import ErrorDetail, ErrorResponse, ValidationIssue

STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    500: "internal_server_error",
    502: "upstream_service_error",
    503: "service_unavailable",
}


def configure_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    assert isinstance(exc, HTTPException)
    detail = _http_error_detail(exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(detail=detail).model_dump(mode="json"),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    assert isinstance(exc, RequestValidationError)
    issues = [
        ValidationIssue(
            location=list(error.get("loc", ())),
            message=str(error.get("msg", "Invalid value")),
            type=str(error.get("type", "value_error")),
        )
        for error in exc.errors()
    ]
    message = issues[0].message if issues else "Request validation failed"
    detail = ErrorDetail(
        code="validation_error",
        message=message,
        msg=message,
        errors=issues,
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(detail=detail).model_dump(mode="json"),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request, exc
    message = "Internal server error"
    detail = ErrorDetail(
        code="internal_server_error",
        message=message,
        msg=message,
    )
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(detail=detail).model_dump(mode="json"),
    )


def _http_error_detail(status_code: int, raw_detail: Any) -> ErrorDetail:
    extras: dict[str, Any] = {}
    code = STATUS_CODES.get(status_code, "http_error")
    try:
        message = HTTPStatus(status_code).phrase
    except ValueError:
        message = "Error"

    if isinstance(raw_detail, str):
        message = raw_detail
    elif isinstance(raw_detail, dict):
        extras = dict(raw_detail)
        code = str(extras.pop("code", code))
        raw_message = extras.pop("message", extras.pop("msg", message))
        message = str(raw_message)
    elif raw_detail is not None:
        message = str(raw_detail)

    return ErrorDetail(
        code=code,
        message=message,
        msg=message,
        **extras,
    )
