"""Resume-point resolution after a human hands control back. Pure logic, exhaustively unit-tested.

The engine re-observes the same live page and asks, for each step from the paused one forward,
whether its postcondition already holds. It resumes after the furthest such step, retries the
paused step if its precondition holds, and otherwise reports a mismatch (-> new intervention).
It never skips over an irreversible step unless the human journal shows the human performed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StepView:
    id: str
    irreversible: bool
    has_postcondition: bool


@dataclass(frozen=True)
class ResumeDecision:
    kind: Literal["resume_at", "retry", "complete", "mismatch"]
    index: int
    human_completed: tuple[str, ...] = ()
    reason: str = ""


def resolve_resume_point(
    steps: list[StepView],
    paused: int,
    post_holds: dict[int, bool],
    pre_holds_paused: bool,
    human_performed: set[str],
) -> ResumeDecision:
    furthest = None
    for k in range(len(steps) - 1, paused - 1, -1):
        if steps[k].has_postcondition and post_holds.get(k, False):
            furthest = k
            break
    if furthest is not None:
        skipped = steps[paused : furthest + 1]
        for s in skipped:
            if s.irreversible and s.id not in human_performed:
                return ResumeDecision(
                    "mismatch", paused, reason=f"irreversible step {s.id} appears done but the journal does not show the human did it"
                )
        done = tuple(s.id for s in skipped)
        if furthest == len(steps) - 1:
            return ResumeDecision("complete", furthest + 1, done, "all remaining postconditions hold")
        return ResumeDecision("resume_at", furthest + 1, done, f"postcondition of {steps[furthest].id} holds")
    if pre_holds_paused:
        return ResumeDecision("retry", paused, (), f"precondition of {steps[paused].id} holds; retrying it")
    return ResumeDecision("mismatch", paused, reason="page matches neither a later postcondition nor the paused step's precondition")
