"""验证审查第 2-6 项涉及的流协议, 接口格式和配置行为."""

from __future__ import annotations

import json
import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

from app.api.routes.models import router as models_router
from app.application.chat import ChatService
from app.application.response_format import response_request
from app.application.response_stream import ResponsesStreamTranslator
from app.application.sse import ChatStreamError, aggregate_sse
from app.config.settings import Settings, duration
from app.domain.auth import Auth
from app.infrastructure.upstream import Upstream


def sse_chunk(**data: object) -> str:
    """构造模拟上游数据事件."""
    return f"data: {json.dumps(data)}\n\n"


def decode_events(events: list[bytes]) -> list[dict]:
    """提取 Responses 事件中的 JSON, 排除结束哨兵."""
    return [
        json.loads(line[5:])
        for event in events
        for line in event.decode().splitlines()
        if line.startswith("data:") and line[5:].strip() != "[DONE]"
    ]


class ResponsesStreamTests(unittest.IsolatedAsyncioTestCase):
    """验证真实聊天服务如何对上游流收尾并释放资源."""

    async def collect(self, source: str) -> list[dict]:
        """通过服务的流式响应入口收集事件并检查连接关闭."""
        response = httpx.Response(200, text=source)
        closed = False

        @asynccontextmanager
        async def upstream_stream(_payload: dict):
            """模拟受管上游连接, 保留服务真实的失败事件处理."""
            nonlocal closed
            try:
                yield response
            finally:
                closed = True
                await response.aclose()

        container = SimpleNamespace(pool=Mock(), settings=Mock(), upstream=Mock(), store=Mock())
        service = ChatService(container)
        with patch.object(service, "_upstream_stream", upstream_stream):
            stream = await service.prepare_stream_response({"model": "demo", "input": "hello"})
            events = [event async for event in stream]
        self.assertTrue(closed)
        self.assertEqual(events[-1], b"data: [DONE]\n\n")
        decoded = decode_events(events)
        self.assertEqual([event["sequence_number"] for event in decoded], list(range(len(decoded))))
        return decoded

    async def test_empty_and_truncated_streams_fail(self):
        """空流, 无候选数据和未收到结束标记的流必须报告失败."""
        partial = sse_chunk(choices=[{"index": 0, "delta": {"content": "partial"}}])
        sources = [
            "", "data: [DONE]\n\n", ": heartbeat\n\ndata: invalid\n\n",
            sse_chunk(choices=[], usage={"total_tokens": 1}) + "data: [DONE]\n\n",
            partial,
            partial + sse_chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        ]
        for source in sources:
            with self.subTest(source=source):
                events = await self.collect(source)
                types = [event["type"] for event in events]
                self.assertNotIn("response.completed", types)
                self.assertEqual(types[-2:], ["error", "response.failed"])
                self.assertEqual(events[-1]["response"]["status"], "failed")

    async def test_complete_stream_preserves_text_and_usage(self):
        """正常结束的流保留增量正文和最后的用量事件."""
        source = sse_chunk(model="demo", choices=[{"index": 0, "delta": {"content": "hello"}}])
        source += sse_chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
        source += sse_chunk(choices=[], usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        events = await self.collect(source + "data: [DONE]\n\n")
        self.assertEqual(events[-1]["type"], "response.completed")
        response = events[-1]["response"]
        self.assertEqual(response["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(response["usage"]["total_tokens"], 3)

    async def test_upstream_error_fails(self):
        """上游明确错误不得被结束标记覆盖."""
        events = await self.collect(sse_chunk(error={"message": "upstream failed"}) + "data: [DONE]\n\n")
        self.assertEqual(events[-1]["type"], "response.failed")

    async def test_done_stops_reading_without_waiting_for_eof(self):
        """收到结束标记后立即完成, 不继续等待或消费上游尾部."""
        async def lines():
            """在结束标记后设置不可消费的哨兵."""
            yield sse_chunk(choices=[{"index": 0, "delta": {"content": "ok"}}]).strip()
            yield "data: [DONE]"
            raise AssertionError("read beyond DONE")

        events = [event async for event in ResponsesStreamTranslator(lines()).translate()]
        self.assertEqual(decode_events(events)[-1]["type"], "response.completed")

    async def test_tool_stream_requires_done(self):
        """工具参数完整与否不能替代协议结束标记."""
        source = sse_chunk(choices=[{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call_a", "function": {"name": "lookup", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}])
        self.assertEqual((await self.collect(source))[-1]["type"], "response.failed")
        events = await self.collect(source + "data: [DONE]\n\n")
        self.assertEqual(events[-1]["response"]["output"][0]["arguments"], "{}")
        self.assertEqual(events[-1]["type"], "response.completed")


class ApiContractTests(unittest.IsolatedAsyncioTestCase):
    """验证模型列表和工具选择参数的实际接口行为."""

    async def test_models_returns_standard_list(self):
        """模型目录在有账号和无账号时均返回顶层标准列表."""
        for ids in (["demo"], []):
            with self.subTest(ids=ids):
                app = FastAPI()
                app.include_router(models_router)
                app.state.container = SimpleNamespace(
                    pool=SimpleNamespace(pick=Mock(return_value=SimpleNamespace(auth=Auth("test")) if ids else None)),
                    upstream=SimpleNamespace(models=AsyncMock(return_value=ids)),
                    store=SimpleNamespace(save_models=AsyncMock()),
                )
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    response = await client.get("/v1/models")
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["object"], "list")
                self.assertEqual([item["id"] for item in body["data"]], ids)
                self.assertNotIn("code", body)

    async def test_tool_choice_none_reaches_both_upstream_paths(self):
        """Responses 转换及两条上游路径均保留禁止工具调用的要求."""
        payload = response_request({
            "model": "demo", "input": "hello", "tool_choice": "none",
            "tools": [{"type": "function", "name": "lookup"}],
        })
        captured = []

        async def handler(request: httpx.Request) -> httpx.Response:
            """记录实际发出的上游 JSON, 不访问外部服务."""
            captured.append(json.loads(request.content))
            return httpx.Response(200, text="data: [DONE]\n\n")

        upstream = Upstream(Settings(_env_file=None), Mock())
        await upstream.client.aclose()
        upstream.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            body = json.dumps(payload).encode()
            response = await upstream.chat(Auth("test"), body)
            await response.aclose()
            async with upstream.chat_stream(Auth("test"), body) as response:
                await response.aread()
        finally:
            await upstream.close()
        self.assertEqual(len(captured), 2)
        for request in captured:
            self.assertEqual(request["tool_choice"], "none")
            self.assertEqual(request["tools"][0]["function"]["name"], "lookup")


class DurationTests(unittest.TestCase):
    """验证环境变量的纯数字时长及原有单位时长."""

    def test_numeric_environment_values(self):
        """字符串环境变量按秒解释, 不得悄悄恢复默认冷却时长."""
        with patch.dict(os.environ, {"LB2A_HARD_CREDIT": "60", "LB2A_SOFT_RATE": "120", "LB2A_ERR_COOLDOWN": "30"}):
            settings = Settings(_env_file=None)
        self.assertEqual((settings.hard_credit, settings.soft_rate, settings.err_cooldown), (60, 120, 30))

    def test_duration_units_and_decimal_seconds(self):
        """保留单位格式, 小数和两侧空白的解析行为."""
        for value, expected in [(60, 60), (" 2.5 ", 2.5), ("60s", 60), ("10m", 600), ("12h", 43200), ("1d", 86400)]:
            with self.subTest(value=value):
                self.assertEqual(duration(value, 999), expected)


class ChatAggregationTests(unittest.IsolatedAsyncioTestCase):
    """验证候选答案按索引独立聚合, 包括同索引的工具调用."""

    async def test_interleaved_choices_keep_independent_state(self):
        """交错候选不得混合文本, 推理, 工具参数或结束原因."""
        source = ""
        for index, text, reason, call_id in [(1, "B", "RB", "call_b"), (0, "A", "RA", "call_a")]:
            source += sse_chunk(choices=[{"index": index, "delta": {
                "content": text, "reasoning_content": reason,
                "tool_calls": [{"index": 0, "id": call_id, "function": {"name": f"tool_{index}", "arguments": '{"x":'}}],
            }}])
        for index, finish in [(0, "tool_calls"), (1, "length")]:
            source += sse_chunk(choices=[{"index": index, "delta": {
                "content": str(index),
                "tool_calls": [{"index": 0, "function": {"arguments": f"{index}}}"}}],
            }, "finish_reason": finish}])
        result = await aggregate_sse(source + "data: [DONE]\n\n")
        self.assertEqual([choice["index"] for choice in result["choices"]], [0, 1])
        for choice, text, reason, call_id, finish in zip(result["choices"], ["A0", "B1"], ["RA", "RB"], ["call_a", "call_b"], ["tool_calls", "length"]):
            message = choice["message"]
            self.assertEqual(message["content"], text)
            self.assertEqual(message["reasoning_content"], reason)
            tool = message["tool_calls"][0]
            self.assertEqual(tool["id"], call_id)
            self.assertEqual(tool["function"]["name"], f"tool_{choice['index']}")
            self.assertEqual(json.loads(tool["function"]["arguments"]), {"x": choice["index"]})
            self.assertEqual(choice["finish_reason"], finish)

    async def test_single_choice_null_content_and_usage(self):
        """单候选首包空内容及尾部用量仍能正确聚合."""
        source = sse_chunk(choices=[{"index": 0, "delta": {"role": "assistant", "content": None}}])
        source += sse_chunk(choices=[{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}])
        source += sse_chunk(choices=[], usage={"total_tokens": 3})
        result = await aggregate_sse(source + "data: [DONE]\n\n")
        self.assertEqual(len(result["choices"]), 1)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(result["usage"]["total_tokens"], 3)

    async def test_empty_and_unterminated_chat_streams_fail(self):
        """聚合过程继续拒绝空响应及缺少结束标记的响应."""
        for source in ["data: [DONE]\n\n", sse_chunk(choices=[{"index": 0, "delta": {"content": "partial"}}])]:
            with self.subTest(source=source), self.assertRaises(ChatStreamError):
                await aggregate_sse(source)


if __name__ == "__main__":
    unittest.main()
