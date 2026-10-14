from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.responses import failure

log = logging.getLogger("lobsterai2api.api")
CHAT_PATHS = frozenset({"/v1/chat/completions", "/v1/responses"})


def _is_chat(request: Request) -> bool:
    return request.url.path in CHAT_PATHS


def _chat_error(message: str, code: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": "api_error", "code": code}},
        status_code=status_code,
    )


def _status_code(exc: StarletteHTTPException) -> int:
    return exc.status_code if 400 <= exc.status_code <= 599 else 500


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status_code = _status_code(exc)
    message = str(exc.detail)
    if _is_chat(request):
        return _chat_error(message, "http_error", status_code)
    return JSONResponse(failure("HTTP_ERROR", message), status_code=status_code)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    if _is_chat(request):
        return _chat_error("invalid request", "invalid_request", 422)
    return JSONResponse(failure("VALIDATION_ERROR", "invalid request", exc.errors()), status_code=422)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled API exception", exc_info=exc)
    if _is_chat(request):
        return _chat_error("internal server error", "internal_error", 500)
    return JSONResponse(failure("INTERNAL_ERROR", "internal server error"), status_code=500)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
