from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionRequest(BaseModel):
    """Validated subset of the OpenAI Chat Completions request."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


class ResponsesRequest(BaseModel):
    """Validated subset of the OpenAI Responses request."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    input: str | list[Any] = ""
    stream: bool = False
