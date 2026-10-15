from __future__ import annotations

import json
import time
from typing import Any


class ChatStreamError(RuntimeError):
    """表示上游 Chat SSE 返回错误事件或没有任何有效数据。"""


def _sse_text(value: Any) -> str:
    """将 SSE 字段转为可拼接字符串.

    上游首包常见 ``content: null``; ``dict.get(key, "")`` 在键存在且值为 None 时仍返回 None.
    """
    return value if isinstance(value, str) else ""


async def aggregate_sse(text: str) -> dict[str, Any]:
    """把上游 chat SSE 聚合成单个 OpenAI chat.completion 响应."""
    result: dict[str, Any] = {
        "id": f"chatcmpl-{time.time_ns()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "",
        "choices": [],
    }
    choices: dict[int, dict[str, Any]] = {}
    received_choice = False
    completed = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            completed = True
            continue
        if not payload:
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            error = chunk["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ChatStreamError(message or "upstream stream error")
        result["id"] = chunk.get("id", result["id"])
        result["created"] = chunk.get("created", result["created"])
        result["model"] = chunk.get("model", result["model"])
        if isinstance(chunk.get("usage"), dict):
            result["usage"] = chunk["usage"]
        for choice in chunk.get("choices", []):
            received_choice = True
            index = int(choice.get("index", 0))
            state = choices.setdefault(index, {
                "index": index,
                "message": {"role": "assistant", "content": ""},
                "content": [],
                "reasoning": [],
                "tool_calls": {},
                "finish_reason": "stop",
            })
            delta = choice.get("delta") or {}
            if delta.get("role"):
                state["message"]["role"] = delta["role"]
            state["content"].append(_sse_text(delta.get("content")))
            state["reasoning"].append(_sse_text(delta.get("reasoning_content")))
            for tool_call in delta.get("tool_calls") or []:
                index = int(tool_call.get("index", 0))
                current = state["tool_calls"].setdefault(index, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tool_call.get("id"):
                    current["id"] = tool_call["id"]
                function = tool_call.get("function") or {}
                current_function = current["function"]
                current_function["name"] += _sse_text(function.get("name"))
                current_function["arguments"] += _sse_text(function.get("arguments"))
            if choice.get("finish_reason"):
                state["finish_reason"] = choice["finish_reason"]
    if not received_choice:
        raise ChatStreamError("upstream returned an empty chat stream")
    if not completed:
        raise ChatStreamError("upstream chat stream ended before [DONE]")
    for index in sorted(choices):
        state = choices[index]
        message = state["message"]
        message["content"] = "".join(state["content"])
        if any(state["reasoning"]):
            message["reasoning_content"] = "".join(state["reasoning"])
        if state["tool_calls"]:
            message["tool_calls"] = [state["tool_calls"][call_index] for call_index in sorted(state["tool_calls"])]
        result["choices"].append({
            "index": index,
            "message": message,
            "finish_reason": state["finish_reason"],
        })
    return result
