from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.application.container import ApplicationContainer
from app.application.login import LoginService


def get_container(request: Request) -> ApplicationContainer:
    return request.app.state.container


def get_login_service(request: Request) -> LoginService:
    return request.app.state.login_service


def is_authorized(container: ApplicationContainer, authorization: str | None) -> bool:
    return not container.settings.api_key or authorization == f"Bearer {container.settings.api_key}"


def unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": {"message": "missing or invalid API key", "type": "api_error", "code": "invalid_api_key"}},
        status_code=401,
    )
