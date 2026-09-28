"""The run result contract: a discriminated union the caller branches on.

``business_outcome`` is a legitimate answer, not an error. ``rejected`` means nothing ran.
``failure`` always says whether a retry is safe (``retryable``) and whether a side effect may have
happened (``side_effect_state``).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SideEffectState = Literal["none", "possible", "committed"]
Mode = Literal["discovery", "replay", "validation"]

REJECT_CODES = {"input_contract_violation", "artifact_invalid", "artifact_not_approved", "policy_denied_preflight"}
FAILURE_CODES = {
    "permission_denied",
    "timeout",
    "app_error",
    "locator_not_found",
    "locator_ambiguous",
    "checkpoint_mismatch",
    "output_invalid",
    "unknown_state",
    "policy_violation",
    "session_recovery_exhausted",
    "app_incompatible",
    "approval_required",
    "approval_denied",
    "discovery_exhausted",
    "internal_error",
}
ABORT_CODES = {"operator_abort", "intervention_timeout"}
RECOVERY_CODES = {
    "interstitial_dismissed",
    "slow_load_waited",
    "transient_retry",
    "session_reauthenticated",
    "dialog_handled",
    "assisted_fallback",
}
EXIT_CODES = {"success": 0, "business_outcome": 10, "rejected": 20, "failure": 30, "aborted": 40}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(_Strict):
    kind: Literal["screenshot", "dom_snapshot", "trace", "events", "review"]
    path: str
    note: str = ""


class Recovery(_Strict):
    code: str
    state_id: str | None = None
    step_id: str | None = None
    detail: str = ""


class DriftSignal(_Strict):
    code: Literal["locator_fallback_used", "fingerprint_minor_mismatch", "assisted_fallback"]
    step_id: str | None = None
    detail: str = ""


class LocatorDiagnostic(_Strict):
    strategy: str
    match_count: int
    detail: str = ""


class InterventionSummary(_Strict):
    intervention_id: str
    kind: str
    step_id: str | None
    reason_code: str
    resolution: str | None
    operator_id: str | None
    human_actions: int = 0


class Usage(_Strict):
    model: str
    decisions: int
    input_tokens: int
    output_tokens: int
    latency_ms: int


class _Base(_Strict):
    run_id: str
    mode: Mode
    capability: str | None = None  # "id@version"
    content_hash: str | None = None
    tenant_id: str | None = None
    started_at: str
    finished_at: str
    duration_ms: int
    evidence: list[EvidenceRef] = Field(default_factory=list)
    interventions: list[InterventionSummary] = Field(default_factory=list)
    usage: Usage | None = None
    overrides: list[str] = Field(default_factory=list)  # tenant override patches applied (ref#hash)


class SuccessResult(_Base):
    status: Literal["success"] = "success"
    outputs: dict[str, Any]
    checkpoint: str
    recoveries: list[Recovery] = Field(default_factory=list)
    drift: list[DriftSignal] = Field(default_factory=list)
    completed_by: dict[str, Literal["automation", "human"]] = Field(default_factory=dict)
    artifact: str | None = None  # discovery: path of the compiled draft
    validation_stop: str | None = None  # validation of irreversible flows stops before this step


class BusinessOutcomeResult(_Base):
    status: Literal["business_outcome"] = "business_outcome"
    code: str
    details: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None
    message: str | None = None
    recoveries: list[Recovery] = Field(default_factory=list)


class FieldError(_Strict):
    field: str
    message: str


class RejectedResult(_Base):
    status: Literal["rejected"] = "rejected"
    code: str
    errors: list[FieldError] = Field(default_factory=list)
    session_opened: Literal[False] = False


class FailureResult(_Base):
    status: Literal["failure"] = "failure"
    category: Literal["hard_failure"] = "hard_failure"
    code: str
    message: str
    retryable: bool
    side_effect_state: SideEffectState
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    attempts: int = 1
    locator_diagnostics: list[LocatorDiagnostic] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    drift: list[DriftSignal] = Field(default_factory=list)


class AbortedResult(_Base):
    status: Literal["aborted"] = "aborted"
    code: str
    actor: str
    reason: str
    last_checkpoint_step: str | None = None
    side_effect_state: SideEffectState = "none"


RunResult = Annotated[
    SuccessResult | BusinessOutcomeResult | RejectedResult | FailureResult | AbortedResult,
    Field(discriminator="status"),
]


class RunResultEnvelope(BaseModel):
    """Wrapper used to generate one JSON Schema for the union."""

    result: RunResult
