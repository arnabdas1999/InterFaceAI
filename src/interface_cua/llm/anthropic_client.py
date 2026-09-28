"""Claude-backed discovery planner (official Anthropic SDK).

One stateless request per decision: stable system prompt + tool (cached) first, then a compact
history summary and the current masked observation. Raw prompts and provider payloads are never
logged; only the validated decision, token usage, and latency are recorded.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import anthropic
from pydantic import ValidationError

from interface_cua.llm.client import DecisionResult, DiscoveryContext, DiscoveryDecision, decision_tool_schema
from interface_cua.llm.prompt import TOOL_DESCRIPTION, TOOL_NAME, prompt_template_hash, render_user_text, system_prompt

__all__ = ["AnthropicPlanner", "render_user_text"]


class AnthropicPlanner:
    def __init__(self, model: str = "claude-sonnet-5", *, max_tokens: int = 4000, effort: str = "medium") -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.prompt_template_hash = prompt_template_hash(decision_tool_schema())
        self._client = anthropic.AsyncAnthropic()
        self._system = system_prompt()
        self._tool: dict[str, Any] = {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "strict": True,
            "input_schema": decision_tool_schema(),
        }

    async def decide(self, ctx: DiscoveryContext) -> DecisionResult:
        image = base64.standard_b64encode(ctx.screenshot_png).decode("ascii")
        started = time.monotonic()
        try:
            response = await self._client.messages.create(  # type: ignore[call-overload]
                model=self.model,
                max_tokens=self.max_tokens,
                system=[{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
                tools=[self._tool],
                tool_choice={"type": "auto"},
                output_config={"effort": self.effort},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}},
                            {"type": "text", "text": render_user_text(ctx)},
                        ],
                    }
                ],
            )
        except anthropic.RateLimitError as exc:
            return DecisionResult(None, f"rate_limited: {exc.message}", self.model)
        except anthropic.APIStatusError as exc:
            return DecisionResult(None, f"api_error {exc.status_code}: {exc.message}", self.model)
        except anthropic.APIConnectionError as exc:
            return DecisionResult(None, f"connection_error: {exc}", self.model)
        latency = int((time.monotonic() - started) * 1000)
        usage = response.usage
        base = {
            "model": response.model,
            "input_tokens": usage.input_tokens + (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0),
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_input_tokens or 0,
            "latency_ms": latency,
            "stop_reason": response.stop_reason,
        }
        if response.stop_reason == "refusal":
            return DecisionResult(None, "model refused the request", **base)
        call = next((b for b in response.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if call is None:
            return DecisionResult(None, f"no act tool call (stop_reason={response.stop_reason})", **base)
        try:
            decision = DiscoveryDecision.model_validate(call.input)
        except ValidationError as exc:
            return DecisionResult(None, f"invalid decision: {exc.errors()[0]['msg']}", **base)
        return DecisionResult(decision, None, **base)
