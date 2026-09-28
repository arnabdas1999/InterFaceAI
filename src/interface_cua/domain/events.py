"""Structured run events (JSONL). Constructed already-redacted; see evidence.logger."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "run_started",
    "session_started",
    "observation",
    "model_decision",
    "model_decision_invalid",
    "policy_decision",
    "action_executed",
    "action_failed",
    "action_blocked",
    "state_detected",
    "recovery_applied",
    "checkpoint",
    "extract",
    "dialog",
    "request_blocked",
    "popup_blocked",
    "drift",
    "intervention_requested",
    "intervention_resolved",
    "lease_transition",
    "human_action",
    "human_input_blocked",
    "resume_point",
    "verification",
    "compile",
    "validation_stop",
    "run_finished",
    "note",
]


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str
    seq: int
    run_id: str
    mode: Literal["discovery", "replay", "validation", "human"]
    capability: str | None = None
    session_id: str | None = None
    step_id: str | None = None
    control_owner: str | None = None
    lease_version: int | None = None
    type: EventType
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
