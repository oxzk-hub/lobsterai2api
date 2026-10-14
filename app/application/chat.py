from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from app.application.container import ApplicationContainer
from app.application.failure_policy import AccountFailurePolicy
from app.application.models import ModelService, map_model
from app.application.response_format import (
    ResponseRequestError,
    chat_to_response,
    response_reasoning,
    response_request,
)
from app.application.response_stream import ResponsesStreamTranslator
from app.application.sse import aggregate_sse
from app.infrastructure.upstream import UpstreamError


class ChatService:
    """协调模型映射、账号重试以及 Chat/Responses 协议转换。"""

    def __init__(self, services: ApplicationContainer):
        """绑定应用依赖并创建聊天所需的领域服务。"""
        self.services = services
        self.failure_policy = AccountFailurePolicy(services.pool, services.settings)
        self.model_service = ModelService(services.pool, services.upstream, services.store)

    @staticmethod
    def _is_request_error(error: UpstreamError) -> bool:
        """判断错误是否由请求参数导致, 因而不应切换账号重试。"""
        return error.kind == "client" and error.status in (400, 422)

    async def _upstream_response(self, payload: dict[str, Any]) -> httpx.Response:
        if not self.services.upstream:
            raise RuntimeError("upstream base URL is not configured")
        body = json.dumps(await self._with_mapped_model(payload), ensure_ascii=False).encode()
        tried: set[str] = set()
        last_error: Exception | None = None
        for _ in range(self.services.settings.max_account_retries):
            entry = self.services.pool.pick(tried)
            if not entry:
                break
            tried.add(entry.auth.uid)
            try:
                response = await self.services.upstream.chat(entry.auth, body)
                entry.errors = 0
                return response
            except UpstreamError as exc:
                # 请求自身导致的 4xx 与账号无关, 重试只会重复失败并污染账号状态。
                if self._is_request_error(exc):
                    raise
                last_error = exc
                await self.failure_policy.record(entry, exc)
        if last_error:
            raise last_error
        raise RuntimeError("all accounts unavailable")

    @asynccontextmanager
    async def _upstream_stream(self, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        if not self.services.upstream:
            raise RuntimeError("upstream base URL is not configured")
        body = json.dumps(await self._with_mapped_model(payload), ensure_ascii=False).encode()
        tried: set[str] = set()
        last_error: Exception | None = None
        for _ in range(self.services.settings.max_account_retries):
            entry = self.services.pool.pick(tried)
            if not entry:
                break
            tried.add(entry.auth.uid)
            entered = False
            try:
                async with self.services.upstream.chat_stream(entry.auth, body) as response:
                    entered = True
                    entry.errors = 0
                    yield response
                    return
            except UpstreamError as exc:
                if entered:
                    raise
                if self._is_request_error(exc):
                    raise
                last_error = exc
                await self.failure_policy.record(entry, exc)
        if last_error:
            raise last_error
        raise RuntimeError("all accounts unavailable")

    async def _with_mapped_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        """选择普通映射模型或显式配置的工具调用模型。"""
        model = payload.get("model", "")
        available = await self.model_service.available_models()
        tool_model = self.services.settings.tool_model.strip()
        if payload.get("tools") and tool_model:
            if available and tool_model not in available:
                raise ResponseRequestError(f"configured tool model is unavailable: {tool_model}")
            mapped_model = tool_model
        else:
            mapped_model = map_model(model, available)
        return {**payload, "model": mapped_model}

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._upstream_response({**payload, "stream": True})
        try:
            return await aggregate_sse(response.text)
        finally:
            await response.aclose()

    async def response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """执行 Responses 请求, 聚合上游流并转换回 Responses 响应。"""
        tool_name_map: dict[str, tuple[str, str]] = {}
        reasoning = response_reasoning(payload)
        chat_payload = response_request(payload, tool_name_map)
        result = await self.chat(chat_payload)
        return chat_to_response(result, payload.get("model", ""), tool_name_map, reasoning)

    async def prepare_stream_chat(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        """在发送响应头前建立上游连接, 并返回受管的 Chat 字节流。"""
        context = self._upstream_stream({**payload, "stream": True})
        response = await context.__aenter__()

        async def body() -> AsyncIterator[bytes]:
            """透传上游字节, 断流时补发 OpenAI Chat 错误事件。"""
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            except Exception as exc:
                error = {
                    "error": {
                        "message": str(exc) or exc.__class__.__name__,
                        "type": "api_error",
                        "param": None,
                        "code": "upstream_error",
                    }
                }
                encoded = json.dumps(error, ensure_ascii=False, separators=(",", ":"))
                yield f"\n\ndata: {encoded}\n\ndata: [DONE]\n\n".encode()
            finally:
                await context.__aexit__(None, None, None)

        return body()

    async def prepare_stream_response(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        """在发送响应头前建立上游连接, 并返回 Responses 事件流。"""
        requested_model = str(payload.get("model") or "")
        tool_name_map: dict[str, tuple[str, str]] = {}
        reasoning = response_reasoning(payload)
        chat_payload = response_request(payload, tool_name_map)
        context = self._upstream_stream({**chat_payload, "stream": True})
        response = await context.__aenter__()
        translator = ResponsesStreamTranslator(
            response.aiter_lines(),
            requested_model,
            response_id=f"resp_{time.time_ns()}",
            tool_name_map=tool_name_map,
            reasoning=reasoning,
        )

        async def body() -> AsyncIterator[bytes]:
            """转换上游事件, 并把流中途异常编码为 Responses 失败事件。"""
            try:
                async for event in translator.translate():
                    yield event
            except Exception as exc:
                for event in translator.failure_events(exc):
                    yield event
            finally:
                await context.__aexit__(None, None, None)

        return body()
