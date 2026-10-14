from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.application.response_format import chat_usage_to_response, reasoning_item


_DONE = b"data: [DONE]\n\n"
"""官方流以 response.completed 结束; 追加 [DONE] 哨兵方便 SSE 解析器收尾。"""


def _event(event_type: str, sequence_number: int, **data: Any) -> bytes:
    """编码单个 Responses SSE 事件, 事件名与 ``data.type`` 保持一致。"""
    payload = {"type": event_type, **data, "sequence_number": sequence_number}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {encoded}\n\n".encode()


def _response_object(
    response_id: str,
    model: str,
    created_at: int,
    status: str,
    output: list[dict[str, Any]],
    *,
    reasoning: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造生命周期事件（created/in_progress/completed/failed）携带的响应对象。"""
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": error,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": reasoning or {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
        "metadata": {},
    }


# 输出项的字段全部按关键字构造, 避免继承链里默认值与必填值的顺序冲突。
@dataclass(kw_only=True)
class _OutputItem:
    """流式输出项的公共状态: 输出位置与完成标记。"""

    output_index: int
    closed: bool = False

    def close_events(self) -> list[tuple[str, dict[str, Any]]]:
        """项关闭时需要发出的事件与参数, 列表顺序即发送顺序。"""
        raise NotImplementedError


@dataclass
class _ReasoningItem(_OutputItem):
    """思维链输出项, 覆盖上游 ``reasoning_content`` 增量。"""

    item_id: str
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def close_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                "response.reasoning_text.done",
                {
                    "item_id": self.item_id,
                    "output_index": self.output_index,
                    "content_index": 0,
                    "text": self.text,
                },
            ),
            (
                "response.output_item.done",
                {
                    "output_index": self.output_index,
                    "item": reasoning_item(self.item_id, self.text),
                },
            ),
        ]


@dataclass
class _MessageItem(_OutputItem):
    """assistant 消息输出项, 覆盖上游 ``content`` 增量。"""

    item_id: str
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.parts)

    def in_progress_item(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }

    def done_item(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": self.text, "annotations": []}],
        }

    def close_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                "response.output_text.done",
                {
                    "item_id": self.item_id,
                    "output_index": self.output_index,
                    "content_index": 0,
                    "text": self.text,
                    "logprobs": [],
                },
            ),
            (
                "response.content_part.done",
                {
                    "item_id": self.item_id,
                    "output_index": self.output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": self.text, "annotations": []},
                },
            ),
            (
                "response.output_item.done",
                {"output_index": self.output_index, "item": self.done_item()},
            ),
        ]


@dataclass
class _ToolCallItem(_OutputItem):
    """function_call 输出项, 覆盖上游 ``tool_calls`` 增量。"""

    item_id: str
    call_id: str
    name: str = ""
    namespace: str | None = None
    arguments: str = ""

    def payload(self, status: str) -> dict[str, Any]:
        """构造 function_call 输出项; 命名空间工具附带 namespace 字段。"""
        item = {
            "id": self.item_id,
            "type": "function_call",
            "status": status,
            "call_id": self.call_id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.namespace is not None:
            item["namespace"] = self.namespace
        return item

    def close_events(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (
                "response.function_call_arguments.done",
                {
                    "item_id": self.item_id,
                    "output_index": self.output_index,
                    "arguments": self.arguments,
                },
            ),
            (
                "response.output_item.done",
                {"output_index": self.output_index, "item": self.payload("completed")},
            ),
        ]


class ResponsesStreamTranslator:
    """把上游 Chat Completions SSE 实时翻译为 Responses 事件流。

    翻译规则:

    - ``reasoning_content`` 增量 → reasoning 输出项 + ``response.reasoning_text.delta``;
    - ``content`` 增量 → assistant 消息项 + ``response.output_text.delta``;
    - ``tool_calls`` 增量 → function_call 输出项 + ``response.function_call_arguments.delta``,
      上游内部名还原为命名空间工具名;
    - 收到 ``finish_reason=tool_calls`` 或流结束时, 按输出顺序补齐全部 done 事件,
      最后发送 ``response.completed`` 与 ``[DONE]`` 哨兵。

    输出项都是惰性创建的: 首个对应增量到达时才发出 added 事件,
    因此事件顺序天然与官方 API 的输出顺序一致（思维链在前, 消息与工具调用在后）。
    """

    def __init__(
        self,
        lines: AsyncIterator[str],
        requested_model: str = "",
        *,
        response_id: str | None = None,
        tool_name_map: dict[str, tuple[str, str]] | None = None,
        reasoning: dict[str, Any] | None = None,
    ):
        """绑定上游 SSE 行迭代器与请求上下文。

        Args:
            lines: 上游 Chat Completions SSE 的 ``aiter_lines()``。
            requested_model: 客户端请求的模型名, 上游未回报时用于回显。
            response_id: 预先生成的响应 ID, 供错误恢复时保持一致。
            tool_name_map: 内部名 → (namespace, name) 的还原表。
            reasoning: 请求中的 reasoning 配置, 原样回显到响应对象。
        """
        self._lines = lines
        self._response_id = response_id or f"resp_{time.time_ns()}"
        self._requested_model = requested_model
        self._tool_name_map = tool_name_map or {}
        self._reasoning = reasoning
        self._created_at = int(time.time())
        self._model = requested_model
        self._sequence = 0
        self._output_count = 0
        self._usage: dict[str, Any] | None = None
        self._reasoning_item: _ReasoningItem | None = None
        self._message_item: _MessageItem | None = None
        self._tool_calls: dict[int, _ToolCallItem] = {}

    def _emit(self, event_type: str, **data: Any) -> bytes:
        """编码事件并推进序号; ``sequence_number`` 从 0 开始单调递增。"""
        event = _event(event_type, self._sequence, **data)
        self._sequence += 1
        return event

    def _next_output_index(self) -> int:
        """分配下一个输出项下标; 项的创建顺序即输出顺序。"""
        index = self._output_count
        self._output_count += 1
        return index

    def _items(self) -> list[_OutputItem]:
        """当前全部输出项, 按输出顺序排列。"""
        items = [item for item in (self._reasoning_item, self._message_item) if item is not None]
        items.extend(self._tool_calls.values())
        return sorted(items, key=lambda item: item.output_index)

    @staticmethod
    def _done_payload(item: _OutputItem) -> dict[str, Any]:
        """输出项在 response.completed 响应对象里的最终形态。"""
        if isinstance(item, _ReasoningItem):
            return reasoning_item(item.item_id, item.text)
        if isinstance(item, _MessageItem):
            return item.done_item()
        return item.payload("completed")

    async def translate(self) -> AsyncIterator[bytes]:
        """逐行消费上游 SSE, 产出完整的 Responses 事件序列。"""
        opened = _response_object(
            self._response_id, self._model, self._created_at, "in_progress", [],
            reasoning=self._reasoning,
        )
        yield self._emit("response.created", response=opened)
        yield self._emit("response.in_progress", response=opened)
        async for line in self._lines:
            chunk = self._parse_line(line)
            if chunk is None:
                continue
            if isinstance(chunk.get("model"), str) and chunk["model"]:
                self._model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                self._usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                for event in self._consume_choice(choice):
                    yield event
        for event in self._finish_output():
            yield event
        completed = _response_object(
            self._response_id, self._model, self._created_at, "completed",
            [self._done_payload(item) for item in self._items()],
            reasoning=self._reasoning,
            usage=chat_usage_to_response(self._usage),
        )
        yield self._emit("response.completed", response=completed)
        yield _DONE

    @staticmethod
    def _parse_line(line: str) -> dict[str, Any] | None:
        """把上游 SSE 行解析为 chunk; 空行、[DONE] 与无效 JSON 返回 None。

        上游返回 error chunk 时抛出 RuntimeError, 由调用方决定如何收尾。
        """
        if not line.startswith("data:"):
            return None
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            return None
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(chunk, dict):
            return None
        if chunk.get("error"):
            error = chunk["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(message or "upstream stream error")
        return chunk

    def _consume_choice(self, choice: dict[str, Any]) -> list[bytes]:
        """翻译单个 choice 增量, 返回本次需要发出的事件。"""
        events: list[bytes] = []
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            events.extend(self._consume_reasoning_delta(reasoning))
        text = delta.get("content")
        if isinstance(text, str) and text:
            events.extend(self._consume_text_delta(text))
        for tool_delta in delta.get("tool_calls") or []:
            events.extend(self._consume_tool_delta(tool_delta))
        if choice.get("finish_reason") == "tool_calls":
            # 上游声明工具调用结束: 立即按输出顺序补齐全部 done 事件,
            # 之后即使还有增量到达也只会被已关闭的项忽略。
            events.extend(self._finish_output())
        return events

    def _consume_reasoning_delta(self, text: str) -> list[bytes]:
        """累积思维链文本; 首个增量到达时创建 reasoning 输出项。"""
        events = self._ensure_reasoning()
        item = self._reasoning_item
        if item is not None and not item.closed:
            item.parts.append(text)
            events.append(self._emit(
                "response.reasoning_text.delta",
                item_id=item.item_id,
                output_index=item.output_index,
                content_index=0,
                delta=text,
            ))
        return events

    def _ensure_reasoning(self) -> list[bytes]:
        """惰性创建 reasoning 输出项并发出 added 事件。"""
        if self._reasoning_item is not None:
            return []
        item = self._reasoning_item = _ReasoningItem(
            item_id=f"rs_{self._response_id}",
            output_index=self._next_output_index(),
        )
        # reasoning 项没有独立的 content_part 事件, added 后直接开始文本增量。
        return [self._emit(
            "response.output_item.added",
            output_index=item.output_index,
            item=reasoning_item(item.item_id, ""),
        )]

    def _consume_text_delta(self, text: str) -> list[bytes]:
        """累积正文文本; 首个增量到达时创建消息项与文本内容块。"""
        events = self._ensure_message()
        item = self._message_item
        if item is not None and not item.closed:
            item.parts.append(text)
            events.append(self._emit(
                "response.output_text.delta",
                item_id=item.item_id,
                output_index=item.output_index,
                content_index=0,
                delta=text,
                logprobs=[],
            ))
        return events

    def _ensure_message(self) -> list[bytes]:
        """惰性创建 assistant 消息项, 先后发出 item 与 content_part 的 added 事件。"""
        if self._message_item is not None:
            return []
        item = self._message_item = _MessageItem(
            item_id=f"msg_{self._response_id}",
            output_index=self._next_output_index(),
        )
        return [
            self._emit(
                "response.output_item.added",
                output_index=item.output_index,
                item=item.in_progress_item(),
            ),
            self._emit(
                "response.content_part.added",
                item_id=item.item_id,
                output_index=item.output_index,
                content_index=0,
                part={"type": "output_text", "text": "", "annotations": []},
            ),
        ]

    def _consume_tool_delta(self, tool_delta: dict[str, Any]) -> list[bytes]:
        """累积单个工具调用增量; 名称可能分片到达, 命名空间在名称完整后还原。"""
        index = int(tool_delta.get("index", 0))
        function = tool_delta.get("function") or {}
        item = self._tool_calls.get(index)
        created = item is None
        if created:
            call_id = str(tool_delta.get("id") or f"call_{self._response_id}_{index}")
            item = self._tool_calls[index] = _ToolCallItem(
                item_id=f"fc_{call_id}",
                call_id=call_id,
                output_index=self._next_output_index(),
            )
        if item.closed:
            return []
        name_fragment = function.get("name")
        if isinstance(name_fragment, str) and name_fragment:
            # 上游可能把函数名拆成多个增量, 拼接完整后再还原命名空间。
            item.name += name_fragment
            if item.namespace is None:
                mapped = self._tool_name_map.get(item.name)
                if mapped is not None:
                    item.namespace, item.name = mapped
        events: list[bytes] = []
        if created:
            events.append(self._emit(
                "response.output_item.added",
                output_index=item.output_index,
                item=item.payload("in_progress"),
            ))
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            item.arguments += arguments
            events.append(self._emit(
                "response.function_call_arguments.delta",
                item_id=item.item_id,
                output_index=item.output_index,
                delta=arguments,
            ))
        return events

    def _finish_output(self) -> list[bytes]:
        """按输出顺序为仍未关闭的项补齐 done 事件。

        上游一般会在 finish_reason=tool_calls 后立即结束, 但部分模型不发送
        finish_reason, 因此流结束时也要兜底执行一次。
        """
        events: list[bytes] = []
        for item in self._items():
            if item.closed:
                continue
            item.closed = True
            for event_type, data in item.close_events():
                events.append(self._emit(event_type, **data))
        return events

    def failure_events(self, exc: Exception) -> list[bytes]:
        """把流中途的异常编码为 error 与 response.failed 事件。

        序号接续当前进度, 保证客户端看到的 sequence_number 连续。
        """
        message = str(exc) or exc.__class__.__name__
        failed = _response_object(
            self._response_id, self._model, self._created_at, "failed", [],
            reasoning=self._reasoning,
            error={"code": "upstream_error", "message": message},
        )
        return [
            self._emit("error", code="upstream_error", message=message, param=None),
            self._emit("response.failed", response=failed),
            _DONE,
        ]
