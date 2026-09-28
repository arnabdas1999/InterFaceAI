"""LLM-backed advisor for replay's bounded assisted fallback.

Lives outside ``interface_cua.replay`` on purpose: replay depends only on the ``FallbackAdvisor``
protocol and never imports a model client. The advisor asks one narrowly-scoped question - "which
control on this (masked) screen performs exactly this recorded step?" - and returns a control number.
Replay decides whether to accept it.
"""

from __future__ import annotations

from typing import Any

from interface_cua.discovery.runner import render_observation
from interface_cua.domain.artifacts import Step
from interface_cua.domain.observations import Observation
from interface_cua.llm.client import DiscoveryContext, LLMClient
from interface_cua.replay.engine import FallbackSuggestion


class LLMFallbackAdvisor:
    def __init__(self, planner: LLMClient) -> None:
        self.planner = planner

    async def suggest(self, *, step: Step, observation: Observation, screenshot: bytes, mask: Any) -> FallbackSuggestion:
        goal = (
            f"RECOVER ONE RECORDED STEP. The step is: '{step.description}' (action: {step.action.value}). "
            "Its recorded control could not be found on this screen; the application may label or build it differently. "
            f"Identify the single control that performs exactly this step and answer with action '{step.action.value}' "
            "on that control's number. If no control clearly performs this step, answer give_up. Do nothing else."
        )
        ctx = DiscoveryContext(
            goal=goal,
            inputs=[],
            route_handles=[],
            observation_text=render_observation(observation, [], mask),
            screenshot_png=screenshot,
            history=[],
            feedback=None,
            step=1,
            max_steps=1,
        )
        result = await self.planner.decide(ctx)
        d = result.decision
        handle = d.handle if d is not None and d.action == step.action.value else None
        why = d.rationale if d is not None else (result.error or "no decision")
        return FallbackSuggestion(handle, why, result.model, result.input_tokens, result.output_tokens)
