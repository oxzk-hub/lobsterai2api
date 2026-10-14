from __future__ import annotations

import json
import time
from typing import Any


class ResponseRequestError(ValueError):
    """表示无法转换为上游 Chat Completions 协议的 Responses 请求。"""


# 上游是 Chat Completions, 无法执行 Responses 服务端托管的搜索工具。
_HOSTED_RESPONSE_TOOL_TYPES = frozenset({"web_search", "web_search_preview"})

# Responses 消息内容块中可以取出纯文本的类型。
_TEXT_CONTENT_TYPES = frozenset({"input_text", "output_text", "text"})


def _content_text(content: Any) -> str:
    """把 Responses 消息内容转换为上游支持的纯文本。

    字符串直接返回; 列表按块拼接, 文本块取 ``text`` 字段, 图片块降级为
    ``[image]`` 占位符——上游不支持多模态输入, 保留占位符可以让带截图的
    请求继续执行而不是直接失败。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponseRequestError("message content must be a string or a list")
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise ResponseRequestError("message content item must be an object")
        part_type = part.get("type")
        if part_type in _TEXT_CONTENT_TYPES:
            parts.append(str(part.get("text") or ""))
        elif part_type == "input_image":
            parts.append("[image]")
        else:
            raise ResponseRequestError(f"unsupported message content type: {part_type!r}")
    return "\n".join(parts)


def _tool_output_text(output: Any) -> str:
    """把函数执行结果稳定地序列化为 Chat tool 消息文本。"""
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def response_reasoning(payload: dict[str, Any]) -> dict[str, Any] | None:
    """读取 Responses 请求的 reasoning 配置, 用于转发 effort 与回显。"""
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    return {"effort": reasoning.get("effort"), "summary": reasoning.get("summary")}


def reasoning_item(item_id: str, text: str) -> dict[str, Any]:
    """构造 Responses reasoning 输出项, 上游思维链文本放入 summary 块。"""
    summary = [{"type": "summary_text", "text": text}] if text else []
    return {"id": item_id, "type": "reasoning", "summary": summary}


def input_to_messages(
    payload: dict[str, Any],
    namespaced_tool_names: dict[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """把 Responses input 项展开为 Chat Completions messages。

    - 字符串 input 视为单条用户消息;
    - message 项按角色转换, developer 归并为 system;
    - function_call 项合并为带 tool_calls 的 assistant 消息,
      带命名空间的工具还原为上游内部名;
    - function_call_output 项转换为 tool 角色消息;
    - reasoning 项承载加密推理, 与其他模型不通用, 直接丢弃。
    """
    value = payload.get("input", "")
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        raise ResponseRequestError("input must be a string or a list")
    namespaced_tool_names = namespaced_tool_names or {}
    messages: list[dict[str, Any]] = []

    def append_tool_call(tool_call: dict[str, Any]) -> None:
        """把 function_call 追加到上一条 assistant 消息, 必要时新建。"""
        if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
            messages[-1]["tool_calls"].append(tool_call)
        else:
            messages.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})

    for item in value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            raise ResponseRequestError("input item must be an object")
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id")
            name = item.get("name")
            if not call_id or not name:
                raise ResponseRequestError("function_call requires call_id and name")
            namespace = item.get("namespace")
            upstream_name = (
                namespaced_tool_names.get((str(namespace), str(name)), str(name))
                if namespace
                else str(name)
            )
            append_tool_call({
                "id": str(call_id),
                "type": "function",
                "function": {
                    "name": upstream_name,
                    "arguments": str(item.get("arguments") or "{}"),
                },
            })
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if not call_id:
                raise ResponseRequestError("function_call_output requires call_id")
            messages.append({
                "role": "tool",
                "tool_call_id": str(call_id),
                "content": _tool_output_text(item.get("output", "")),
            })
        elif item_type == "reasoning":
            continue
        elif item_type == "message" or item.get("role"):
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            messages.append({"role": role, "content": _content_text(item.get("content", ""))})
        else:
            raise ResponseRequestError(f"unsupported input item type: {item_type!r}")
    return messages


def _chat_function_tool(tool: dict[str, Any], name: str) -> dict[str, Any]:
    """把单个 Responses 函数工具转换为 Chat Completions 工具。"""
    function: dict[str, Any] = {
        "name": name,
        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
    }
    if tool.get("description") is not None:
        function["description"] = str(tool["description"])
    if tool.get("strict") is not None:
        function["strict"] = bool(tool["strict"])
    return {"type": "function", "function": function}


def _namespaced_chat_name(namespace_index: int, tool_index: int) -> str:
    """生成符合 Chat 函数名约束且不会与用户函数重名的内部名称。"""
    return f"__lb2a_namespace_{namespace_index}_{tool_index}"


def response_tools_to_chat(
    tools: Any,
    tool_name_map: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """把 Responses 函数和命名空间工具转换为 Chat Completions 工具。

    命名空间内的工具会改用内部名发送给上游, 转换关系写入 ``tool_name_map``,
    供响应方向还原 ``namespace.name``。
    """
    if not isinstance(tools, list):
        raise ResponseRequestError("tools must be a list")
    tool_name_map = tool_name_map if tool_name_map is not None else {}
    converted: list[dict[str, Any]] = []
    direct_names = {
        str(tool.get("name"))
        for tool in tools
        if isinstance(tool, dict) and tool.get("type") == "function" and tool.get("name")
    }
    for namespace_index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise ResponseRequestError("tool must be an object")
        tool_type = tool.get("type")
        if tool_type == "function":
            name = tool.get("name")
            if not name:
                raise ResponseRequestError("function tool requires name")
            converted.append(_chat_function_tool(tool, str(name)))
            continue
        if tool_type == "namespace":
            namespace = tool.get("name")
            namespace_tools = tool.get("tools")
            if not namespace:
                raise ResponseRequestError("namespace tool requires name")
            if not isinstance(namespace_tools, list):
                raise ResponseRequestError("namespace tools must be a list")
            for tool_index, nested_tool in enumerate(namespace_tools):
                if not isinstance(nested_tool, dict):
                    raise ResponseRequestError("namespace tool item must be an object")
                if nested_tool.get("type") != "function":
                    raise ResponseRequestError(
                        f"unsupported namespace tool type: {nested_tool.get('type')!r}"
                    )
                nested_name = nested_tool.get("name")
                if not nested_name:
                    raise ResponseRequestError("namespace function tool requires name")
                upstream_name = _namespaced_chat_name(namespace_index, tool_index)
                while upstream_name in direct_names or upstream_name in tool_name_map:
                    upstream_name = f"_{upstream_name}"
                tool_name_map[upstream_name] = (str(namespace), str(nested_name))
                converted.append(_chat_function_tool(nested_tool, upstream_name))
            continue
        if tool_type in _HOSTED_RESPONSE_TOOL_TYPES:
            # 上游是 Chat Completions, 无法执行 Responses 服务端托管的搜索工具。
            continue
        raise ResponseRequestError(f"unsupported tool type: {tool_type!r}")
    return converted


def response_tool_choice_to_chat(tool_choice: Any) -> Any:
    """把 Responses tool_choice 转换为 Chat Completions 格式。"""
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") in _HOSTED_RESPONSE_TOOL_TYPES:
        return None
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": str(tool_choice["name"])}}
    raise ResponseRequestError("unsupported tool_choice")


def response_request(
    payload: dict[str, Any],
    tool_name_map: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """把 OpenAI Responses 请求转换为上游 Chat Completions 请求。"""
    tool_name_map = tool_name_map if tool_name_map is not None else {}
    converted_tools = None
    if "tools" in payload:
        converted_tools = response_tools_to_chat(payload["tools"], tool_name_map)
    namespaced_tool_names = {
        namespaced_name: upstream_name
        for upstream_name, namespaced_name in tool_name_map.items()
    }
    request: dict[str, Any] = {
        "model": payload.get("model", ""),
        "messages": input_to_messages(payload, namespaced_tool_names),
        "stream": bool(payload.get("stream", True)),
    }
    for key in ("temperature", "top_p", "max_output_tokens", "parallel_tool_calls"):
        if key in payload:
            request["max_tokens" if key == "max_output_tokens" else key] = payload[key]
    # Responses 的 reasoning.effort 对应 Chat 的 reasoning_effort;
    # 不支持该字段的上游一般会直接忽略。
    effort = (response_reasoning(payload) or {}).get("effort")
    if effort:
        request["reasoning_effort"] = str(effort)
    if converted_tools:
        request["tools"] = converted_tools
    if converted_tools and "tool_choice" in payload:
        converted_tool_choice = response_tool_choice_to_chat(payload["tool_choice"])
        if converted_tool_choice is not None:
            request["tool_choice"] = converted_tool_choice
    if payload.get("instructions"):
        request["messages"].insert(0, {"role": "system", "content": payload["instructions"]})
    if not request["messages"]:
        raise ResponseRequestError("input produced no upstream messages")
    return request


def chat_to_response(
    chat: dict[str, Any],
    requested_model: str = "",
    tool_name_map: dict[str, tuple[str, str]] | None = None,
    reasoning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把聚合后的 Chat Completions 响应转换为 Responses 响应。

    思维链文本（``reasoning_content``）生成独立的 reasoning 输出项并排在
    最前; 工具调用生成 function_call 输出项, 内部名还原为命名空间工具名。
    """
    tool_name_map = tool_name_map or {}
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    response_id = chat.get("id", f"resp_{time.time_ns()}")
    output: list[dict[str, Any]] = []
    if message.get("reasoning_content"):
        output.append(reasoning_item(f"rs_{response_id}", str(message["reasoning_content"])))
    if text or not message.get("tool_calls"):
        output.append({
            "id": f"msg_{response_id}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for index, tool_call in enumerate(message.get("tool_calls") or []):
        function = tool_call.get("function") or {}
        call_id = tool_call.get("id") or f"call_{response_id}_{index}"
        upstream_name = str(function.get("name") or "")
        namespace, name = tool_name_map.get(upstream_name, (None, upstream_name))
        item = {
            "id": f"fc_{call_id}",
            "type": "function_call",
            "status": "completed",
            "call_id": call_id,
            "name": name,
            "arguments": function.get("arguments") or "{}",
        }
        if namespace is not None:
            item["namespace"] = namespace
        output.append(item)
    return {
        "id": response_id,
        "object": "response",
        "created_at": chat.get("created", int(time.time())),
        "status": "completed",
        "model": chat.get("model") or requested_model,
        "output": output,
        "output_text": text,
        "error": None,
        "incomplete_details": None,
        "metadata": {},
        "parallel_tool_calls": True,
        "reasoning": reasoning or {"effort": None, "summary": None},
        "usage": chat_usage_to_response(chat.get("usage")),
    }


def chat_usage_to_response(usage: Any) -> dict[str, Any] | None:
    """把 Chat token 用量字段转换为 Responses token 用量字段。"""
    if not isinstance(usage, dict) or not usage:
        return None
    if "input_tokens" in usage:
        return usage
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }
