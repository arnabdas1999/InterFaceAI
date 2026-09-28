"""Discovery planner for any OpenAI-compatible chat-completions endpoint.

Used for Google Gemini (free tier) through its OpenAI-compatible endpoint, and equally for Ollama,
Groq, or OpenRouter by changing the base URL, key, and model in .env. Same prompt, same decision
contract, same masking: only the transport differs from the Claude planner.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import openai
from pydantic import ValidationError

from interface_cua.llm.client import DecisionResult, DiscoveryContext, DiscoveryDecision, coerce_decision, portable_tool_schema
from interface_cua.llm.prompt import TOOL_DESCRIPTION, TOOL_NAME, prompt_template_hash, render_user_text, system_prompt


class OpenAICompatiblePlanner:
    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        max_tokens: int = 4000,
        reasoning_effort: str | None = None,
        max_retries: int = 6,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        schema = portable_tool_schema()
        self.prompt_template_hash = prompt_template_hash(schema)
        # max_retries: free tiers rate-limit; the SDK backs off on 429/5xx and honours retry-after.
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=max_retries, timeout=120)
        self._system = system_prompt()
        self._tools: list[dict[str, Any]] = [
            {"type": "function", "function": {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "parameters": schema}}
        ]

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        return sorted(m.id for m in page.data)

    async def decide(self, ctx: DiscoveryContext) -> DecisionResult:
        image = base64.standard_b64encode(ctx.screenshot_png).decode("ascii")
        extra: dict[str, Any] = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        started = time.monotonic()
        try:
            response = await self._client.chat.completions.create(  # type: ignore[call-overload]
                model=self.model,
                max_tokens=self.max_tokens,
                tools=self._tools,
                tool_choice="auto",
                messages=[
                    {"role": "system", "content": self._system},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                            {"type": "text", "text": render_user_text(ctx)},
                        ],
                    },
                ],
                **extra,
            )
        except openai.RateLimitError as exc:
            return DecisionResult(None, f"rate_limited: {exc.message}", self.model)
        except openai.APIStatusError as exc:
            return DecisionResult(None, f"api_error {exc.status_code}: {exc.message}", self.model)
        except openai.APIConnectionError as exc:
            return DecisionResult(None, f"connection_error: {exc}", self.model)
        latency = int((time.monotonic() - started) * 1000)
        usage = response.usage
        choice = response.choices[0] if response.choices else None
        model = response.model or self.model
        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0
        finish = choice.finish_reason if choice else None

        def result(decision: DiscoveryDecision | None, error: str | None) -> DecisionResult:
            return DecisionResult(
                decision, error, model, input_tokens=in_tok, output_tokens=out_tok, latency_ms=latency, stop_reason=finish
            )

        if choice is None:
            return result(None, "empty response")
        calls = [c for c in (choice.message.tool_calls or []) if c.type == "function" and c.function.name == TOOL_NAME]
        if not calls:
            return result(None, f"no act tool call (finish_reason={choice.finish_reason})")
        try:
            raw = json.loads(calls[0].function.arguments or "{}")
            if not isinstance(raw, dict):
                raise ValueError("arguments are not an object")
            decision = coerce_decision(raw)
        except (ValueError, ValidationError) as exc:
            msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            return result(None, f"invalid decision: {msg}")
        return result(decision, None)
