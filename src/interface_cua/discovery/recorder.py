"""The executed-action trajectory. The compiler reads this, never the model transcript.

It holds no concrete input values (only ``from_input`` references) and no extracted values.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.actions import ActionKind, RiskClass
from interface_cua.domain.conditions import ValueRef
from interface_cua.domain.targets import FrameRef, TargetSpec
from interface_cua.domain.types import OutputType


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameState(_M):
    frame_path: list[FrameRef]
    route: str


class ExtractRecord(_M):
    output_name: str
    output_type: OutputType
    label_text: str
    target: TargetSpec


class RecordedStep(_M):
    index: int
    provenance: Literal["model", "human"]
    action: ActionKind
    target: TargetSpec | None = None
    value: ValueRef | None = None
    select_by: Literal["value", "label"] | None = None
    key: str | None = None
    route_handle: str | None = None
    risk: RiskClass
    intent_risk: str
    submits_form: bool = False
    element_role: str | None = None
    route_before: str
    route_after: str
    frames_after: list[FrameState] = Field(default_factory=list)
    dialog_states: list[str] = Field(default_factory=list)
    recoveries: list[str] = Field(default_factory=list)
    extract: ExtractRecord | None = None
    description: str
    rationale: str = ""
    elapsed_ms: int = 0
    dead_end: bool = False  # set when a later step shows this one did not contribute
    state_changed: bool = True  # the observed screen fingerprint changed (clicks that change nothing are dropped)


class InputDecl(_M):
    name: str
    type: Literal["string", "integer", "enum"] = "string"
    description: str = ""
    pattern: str | None = None
    enum: list[str] | None = None
    sensitivity: Literal["public", "pii_low", "pii", "financial"] = "pii_low"


class OutputDecl(_M):
    name: str
    type: OutputType


class DiscoveryRequest(_M):
    goal: str
    tenant_id: str
    entry: str
    capability_id: str
    capability_name: str | None = None
    inputs: list[InputDecl] = Field(default_factory=list)
    expected_outputs: list[OutputDecl] = Field(default_factory=list)
    max_steps: int = 25
    timeout_s: int = 300
    escalation_mode: Literal["escalate", "fail_fast"] = "fail_fast"
    headed: bool = False


class Trajectory(_M):
    run_id: str
    request: DiscoveryRequest
    model: str
    prompt_template_hash: str
    steps: list[RecordedStep]
    final_route: str
    final_frames: list[FrameState]
    checkpoint: list[dict[str, Any]]  # verified proposals: {label_text, frame_path, equals_input|equals_text}
    outputs: list[OutputDecl]
    extract: list[ExtractRecord]
    observed_states: list[str] = Field(default_factory=list)
    routes_seen: list[str] = Field(default_factory=list)
    extract_after: dict[str, int] = Field(default_factory=dict)
