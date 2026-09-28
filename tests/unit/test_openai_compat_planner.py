"""The OpenAI-compatible planner (Gemini free tier, Ollama, ...) against a local fake endpoint."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request

from interface_cua.llm.client import DeclaredInput, DiscoveryContext, coerce_decision, portable_tool_schema
from interface_cua.llm.openai_compat_client import OpenAICompatiblePlanner

pytestmark = pytest.mark.unit
PORT = 8799
SEEN: list[dict[str, Any]] = []
REPLY: dict[str, Any] = {}


def _server() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> dict[str, Any]:
        SEEN.append(await request.json())
        return {
            "id": "x", "object": "chat.completion", "created": 0, "model": "fake-gemini",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "act", "arguments": json.dumps(REPLY)}}]}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 40, "total_tokens": 1240},
        }  # fmt: skip

    return app


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    server = uvicorn.Server(uvicorn.Config(_server(), host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{PORT}/v1"
    server.should_exit = True


def ctx() -> DiscoveryContext:
    return DiscoveryContext(goal="read balance", inputs=[DeclaredInput("member_id", "string", "5-digit id", "pii_low")],
                            route_handles=["member_search"], observation_text="Member ID ⟦input:member_id⟧", screenshot_png=b"\x89PNG",
                            history=[], feedback=None, step=1, max_steps=5)  # fmt: skip


def test_portable_schema_has_no_nullable_unions() -> None:
    text = json.dumps(portable_tool_schema())
    assert "null" not in text and '"type": ["' not in text
    assert set(portable_tool_schema()["required"]) == {"action", "intent_risk", "rationale"}  # type: ignore[arg-type]


def test_coerce_decision_tolerates_loose_provider_output() -> None:
    d = coerce_decision({"action": "click", "handle": 7, "input_name": "", "key": "", "selector": "#x", "rationale": "go",
                         "intent_risk": "reversible", "checkpoint": []})  # fmt: skip
    assert d.handle == "7" and d.input_name is None and d.key is None and d.checkpoint is None
    with pytest.raises(ValueError):
        coerce_decision({"action": "run_js", "rationale": "x", "intent_risk": "reversible"})


async def test_planner_round_trip_through_openai_compatible_endpoint(endpoint: str) -> None:
    REPLY.clear()
    REPLY.update({"action": "type", "handle": "3", "input_name": "member_id", "intent_risk": "reversible", "rationale": "enter id"})
    planner = OpenAICompatiblePlanner("fake-gemini", base_url=endpoint, api_key="test-key", max_retries=0)
    result = await planner.decide(ctx())
    assert result.error is None and result.decision is not None
    assert result.decision.action == "type" and result.decision.input_name == "member_id"
    assert result.input_tokens == 1200 and result.output_tokens == 40
    body = SEEN[-1]
    assert body["tools"][0]["function"]["name"] == "act" and body["tool_choice"] == "auto"
    user = body["messages"][1]["content"]
    assert user[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "⟦input:member_id⟧" in user[1]["text"] and "<<<PAGE" in user[1]["text"]


async def test_planner_reports_invalid_tool_arguments(endpoint: str) -> None:
    REPLY.clear()
    REPLY.update({"action": "teleport", "rationale": "x", "intent_risk": "reversible"})
    planner = OpenAICompatiblePlanner("fake-gemini", base_url=endpoint, api_key="test-key", max_retries=0)
    result = await planner.decide(ctx())
    assert result.decision is None and result.error and result.error.startswith("invalid decision")
