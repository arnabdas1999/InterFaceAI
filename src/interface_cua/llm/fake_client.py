"""Deterministic planner for offline tests. It cannot stand in for the genuine discovery evidence."""

from __future__ import annotations

from collections.abc import Callable

from interface_cua.llm.client import DecisionResult, DiscoveryContext, DiscoveryDecision

Script = list[DiscoveryDecision | str] | Callable[[DiscoveryContext], DiscoveryDecision | str]


class FakePlanner:
    """Replays a script of decisions. A ``str`` entry simulates an invalid model response.

    A callable script receives the context (masked observation text) and chooses, which lets tests
    pick handles by control name without hard-coding numbers.
    """

    prompt_template_hash = "fake"

    def __init__(self, script: Script, model: str = "fake-planner") -> None:
        self.script = script
        self.model = model
        self.calls: list[DiscoveryContext] = []
        self._i = 0

    async def decide(self, ctx: DiscoveryContext) -> DecisionResult:
        self.calls.append(ctx)
        if callable(self.script):
            item = self.script(ctx)
        elif self._i < len(self.script):
            item = self.script[self._i]
        else:
            item = DiscoveryDecision(action="give_up", rationale="script exhausted")
        self._i += 1
        if isinstance(item, str):
            return DecisionResult(None, item, self.model, input_tokens=100, output_tokens=10)
        return DecisionResult(item, None, self.model, input_tokens=100, output_tokens=20, latency_ms=1)
