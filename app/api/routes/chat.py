from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.dependencies import get_container
from app.api.schemas import ChatCompletionRequest, ResponsesRequest
from app.application.chat import ChatService
from app.application.response_format import ResponseRequestError
from app.infrastructure.upstream import UpstreamError

router = APIRouter()

STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
"""SSE 流式响应头: 禁用客户端缓存与反向代理缓冲, 保证事件实时到达。"""


def service_error(exc: Exception, status_code: int | None = None) -> JSONResponse:
    """把聊天服务异常转换为 OpenAI 风格错误响应。"""
    if status_code is None:
        status_code = 400 if isinstance(exc, ResponseRequestError) else 503
        if isinstance(exc, UpstreamError) and exc.kind == "client":
            status_code = exc.status if 400 <= exc.status < 500 else 400
    is_client_error = 400 <= status_code < 500
    error_type = "invalid_request_error" if is_client_error else "api_error"
    error_code = "invalid_request_error" if is_client_error else "upstream_error"
    return JSONResponse(
        {
            "error": {
                "message": str(exc),
                "type": error_type,
                "param": None,
                "code": error_code,
            }
        },
        status_code=status_code,
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatCompletionRequest) -> Any:
    container = get_container(request)
    payload = body.model_dump()
    service = ChatService(container)
    try:
        if body.stream:
            stream = await service.prepare_stream_chat(payload)
            return StreamingResponse(stream, media_type="text/event-stream", headers=STREAM_HEADERS)
        return await service.chat(payload)
    except Exception as exc:
        return service_error(exc)


@router.post("/v1/responses")
async def responses(request: Request, body: ResponsesRequest) -> Any:
    container = get_container(request)
    payload = body.model_dump()
    service = ChatService(container)
    try:
        if body.stream:
            stream = await service.prepare_stream_response(payload)
            return StreamingResponse(stream, media_type="text/event-stream", headers=STREAM_HEADERS)
        return await service.response(payload)
    except Exception as exc:
        return service_error(exc)
