"""Intervention requests and the control lease (who is, and who should be, in control)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ControlState(StrEnum):
    AUTOMATION_ACTIVE = "AUTOMATION_ACTIVE"
    PAUSING = "PAUSING"
    HUMAN_PENDING = "HUMAN_PENDING"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


ALLOWED_TRANSITIONS: dict[ControlState, set[ControlState]] = {
    ControlState.AUTOMATION_ACTIVE: {ControlState.PAUSING, ControlState.COMPLETED, ControlState.FAILED, ControlState.ABORTED},
    ControlState.PAUSING: {ControlState.HUMAN_PENDING},
    ControlState.HUMAN_PENDING: {ControlState.HUMAN_ACTIVE, ControlState.ABORTED, ControlState.RESUMING},
    ControlState.HUMAN_ACTIVE: {
        ControlState.RESUMING,
        ControlState.COMPLETED,
        ControlState.ABORTED,
        ControlState.HUMAN_PENDING,  # heartbeat lost
    },
    ControlState.RESUMING: {ControlState.AUTOMATION_ACTIVE, ControlState.HUMAN_PENDING, ControlState.COMPLETED},
    ControlState.COMPLETED: set(),
    ControlState.FAILED: set(),
    ControlState.ABORTED: set(),
}


class LeaseSnapshot(_Strict):
    session_id: str
    state: ControlState
    owner: str  # "automation" | "human:<operator_id>" | "unassigned"
    expected_owner: str
    version: int  # fencing token; bumps on every change
    expires_at: float | None = None
    last_heartbeat: float | None = None


InterventionKind = Literal["stuck", "unknown_state", "approval_required", "locator_failure", "discovery_stuck"]
ResolutionKind = Literal["resume", "resume_at_step", "complete", "abort", "approve", "deny"]


class Resolution(_Strict):
    kind: ResolutionKind
    operator_id: str
    note: str = ""
    step_id: str | None = None
    action_hash: str | None = None  # approvals must echo the exact bound-action hash


class Intervention(_Strict):
    id: str
    run_id: str
    kind: InterventionKind
    capability: str | None = None
    goal: str | None = None
    tenant_id: str | None = None
    step_id: str | None = None
    step_description: str | None = None
    bound_action: str | None = None  # sanitized summary of the action at stake
    bound_action_hash: str | None = None
    last_checkpoint_step: str | None = None
    reason_code: str
    explanation: str
    route: str | None = None
    screenshot: str | None = None
    expected: str | None = None
    observed: str | None = None
    control: LeaseSnapshot
    permitted_resolutions: list[ResolutionKind]
    claim_deadline: float
    max_human_active_s: int
    approval_valid_s: int = 300
    created_at: float
    notified_at: float | None = None
    claimed_at: float | None = None
    resolved_at: float | None = None
    operator_id: str | None = None
    resolution: Resolution | None = None
    status: Literal["open", "claimed", "resolved", "expired"] = "open"
    human_actions: list[dict[str, object]] = Field(default_factory=list)
