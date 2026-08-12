"""Small Pydantic AI test helpers shared by Compass backend tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.usage import RequestUsage


def output_tool_response(
    info: AgentInfo,
    payload: Mapping[str, Any],
    *,
    usage: RequestUsage | None = None,
    provider_name: str | None = None,
    finish_reason: str | None = None,
) -> ModelResponse:
    """Return a FunctionModel response that calls the agent's output tool."""

    output_tool = info.output_tools[0]
    kwargs: dict[str, Any] = {}
    if usage is not None:
        kwargs["usage"] = usage
    if provider_name is not None:
        kwargs["provider_name"] = provider_name
    if finish_reason is not None:
        kwargs["finish_reason"] = finish_reason
    return ModelResponse(
        parts=[
            ToolCallPart(
                output_tool.name,
                dict(payload),
                tool_call_id=f"pyd_ai_tool_call_id__{output_tool.name}",
            )
        ],
        **kwargs,
    )
