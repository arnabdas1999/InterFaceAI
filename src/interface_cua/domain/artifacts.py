"""The capability artifact: a typed, versioned, reviewable, agent-invocable description of a flow.

Content (everything except ``content_hash`` and ``lifecycle``) is immutable once written and hashed.
The lifecycle record (draft -> validated -> approved -> deprecated) is append-only and bound to the
hash it approved, so an edited artifact can never inherit an old approval.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from interface_cua.domain.actions import ActionKind, RiskClass
from interface_cua.domain.canonical import sha256_of
from interface_cua.domain.conditions import Condition, ValueRef
from interface_cua.domain.targets import TargetSpec
from interface_cua.domain.types import OutputType

SCHEMA_VERSION: Literal["1.0"] = "1.0"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

Sensitivity = Literal["public", "pii_low", "pii", "financial", "secret"]
Status = Literal["draft", "validated", "approved", "deprecated"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CapabilityInfo(_Strict):
    id: str
    name: str
    description: str
    version: str

    @model_validator(mode="after")
    def _check(self) -> CapabilityInfo:
        if not ID_RE.match(self.id):
            raise ValueError(f"capability id {self.id!r} must be kebab-case")
        if not SEMVER_RE.match(self.version):
            raise ValueError(f"version {self.version!r} is not semver MAJOR.MINOR.PATCH")
        return self


class EntryPoint(_Strict):
    route_handle: str
    route: str


class TargetApp(_Strict):
    vendor_product: str
    app_profile: str
    app_profile_version_range: str
    surface_type: Literal["web", "legacy-web", "desktop"]
    entry_point: EntryPoint
    requires_session: Literal["authenticated", "none"]
    adapter_requirements: list[str] = Field(default_factory=list)


class BusinessOutcomeDecl(_Strict):
    code: str
    description: str
    details_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class Contract(_Strict):
    """What a calling agent sees. ``inputs``/``outputs`` are JSON Schema objects, so a contract
    maps 1:1 to a tool definition."""

    inputs: dict[str, Any]
    outputs: dict[str, Any]
    business_outcomes: list[BusinessOutcomeDecl] = Field(default_factory=list)
    side_effects: Literal["none", "reversible", "irreversible"]

    def input_names(self) -> set[str]:
        return set(self.inputs.get("properties", {}))

    def output_names(self) -> set[str]:
        return set(self.outputs.get("properties", {}))

    def input_sensitivity(self, name: str) -> Sensitivity:
        prop = self.inputs.get("properties", {}).get(name, {})
        value = prop.get("x-sensitivity", "pii")
        return value  # type: ignore[no-any-return]


class ArtifactPolicy(_Strict):
    """Narrowing only: intersected with global and tenant policy at run time."""

    allowed_actions: list[ActionKind]
    allowed_routes: list[str]


class StateMapping(_Strict):
    """How this capability responds to a catalog state at this step."""

    state: str
    on: Literal["business_outcome", "handle", "escalate", "fail"]
    outcome_code: str | None = None


class RetryPolicy(_Strict):
    max_attempts: int = Field(ge=1, le=3)
    backoff_ms: int = Field(ge=0, le=10_000)


class Step(_Strict):
    id: str
    description: str
    provenance: Literal["model", "human", "compiler", "authored"]
    action: ActionKind
    target: TargetSpec | None = None
    value: ValueRef | None = None
    select_by: Literal["value", "label"] | None = None
    key: str | None = None
    route_handle: str | None = None
    risk_class: RiskClass
    idempotent: bool
    precondition: Condition | None = None
    postcondition: Condition | None = None
    timeout_ms: int = Field(default=10_000, ge=500, le=120_000)
    retry: RetryPolicy | None = None
    states: list[StateMapping] = Field(default_factory=list)
    reentry_point: bool = False
    review_required: bool = False
    review_notes: list[str] = Field(default_factory=list)
    rationale: str = ""
    on_success: str = "end"


class Extractor(_Strict):
    output: str
    after_step: str
    target: TargetSpec
    method: Literal["text", "input_value", "attribute"] = "text"
    attribute: str | None = None
    capture: str | None = None  # regex with one group applied to the raw text before normalizing
    type: OutputType
    enum_values: list[str] | None = None
    sensitivity: Sensitivity = "pii"


class SuccessCondition(_Strict):
    description: str
    checks: list[Condition]


class ReviewInfo(_Strict):
    review_required: bool
    items: list[str] = Field(default_factory=list)


class Provenance(_Strict):
    source_run_id: str | None
    discovery_goal: str | None
    model: str | None
    prompt_template_hash: str | None
    compiler_version: str
    created_at: str
    parent: str | None = None  # "id@version" this was derived from
    authored_patches: list[str] = Field(default_factory=list)
    executed_actions: int = 0
    dropped_actions: int = 0


class Compatibility(_Strict):
    app_profile: str
    product: str
    version_range: str
    base_tenant: str
    tenant_override_refs: list[str] = Field(default_factory=list)


class LifecycleTransition(_Strict):
    to: Status
    at: str
    actor: str
    note: str = ""
    run_ids: list[str] = Field(default_factory=list)


class Approval(_Strict):
    approver: str
    at: str
    content_hash: str
    note: str = ""


class Lifecycle(_Strict):
    status: Status = "draft"
    transitions: list[LifecycleTransition] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)


class CapabilityArtifact(_Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability: CapabilityInfo
    target: TargetApp
    contract: Contract
    policy: ArtifactPolicy
    steps: list[Step]
    extract: list[Extractor] = Field(default_factory=list)
    success: SuccessCondition
    review: ReviewInfo
    provenance: Provenance
    compatibility: Compatibility
    content_hash: str = ""
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)

    # --- identity -----------------------------------------------------------
    @property
    def ref(self) -> str:
        return f"{self.capability.id}@{self.capability.version}"

    def content_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"content_hash", "lifecycle"}, exclude_none=True)

    def compute_hash(self) -> str:
        return sha256_of(self.content_dict())

    def with_hash(self) -> CapabilityArtifact:
        return self.model_copy(update={"content_hash": self.compute_hash()})

    def hash_ok(self) -> bool:
        return bool(self.content_hash) and self.content_hash == self.compute_hash()

    def is_approved(self) -> bool:
        return (
            self.lifecycle.status == "approved"
            and bool(self.lifecycle.approvals)
            and self.lifecycle.approvals[-1].content_hash == self.content_hash
            and self.hash_ok()
        )

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def step_index(self, step_id: str) -> int:
        return [s.id for s in self.steps].index(step_id)

    # --- structural validation ------------------------------------------------
    @model_validator(mode="after")
    def _structure(self) -> CapabilityArtifact:
        errors: list[str] = []
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            errors.append("step ids must be unique")
        if not self.steps:
            errors.append("an artifact needs at least one step")
        inputs = self.contract.input_names()
        outputs = self.contract.output_names()
        outcome_codes = {o.code for o in self.contract.business_outcomes}
        for s in self.steps:
            if s.on_success != "end" and s.on_success not in ids:
                errors.append(f"{s.id}: on_success -> unknown step {s.on_success}")
            for ref in _value_refs(s):
                for name in (ref.from_input, ref.vocab_from_input):
                    if name and name not in inputs:
                        errors.append(f"{s.id}: binds undeclared input {name}")
            if s.retry is not None and not s.idempotent:
                errors.append(f"{s.id}: retry policy on a non-idempotent step")
            if s.risk_class == RiskClass.IRREVERSIBLE:
                if s.idempotent:
                    errors.append(f"{s.id}: irreversible step cannot be idempotent")
                if self.contract.side_effects != "irreversible":
                    errors.append(f"{s.id}: irreversible step but contract.side_effects != irreversible")
            for m in s.states:
                if m.on == "business_outcome" and m.outcome_code not in outcome_codes:
                    errors.append(f"{s.id}: maps {m.state} to undeclared outcome {m.outcome_code}")
            if s.action in {ActionKind.CLICK, ActionKind.TYPE, ActionKind.SELECT} and s.target is None:
                errors.append(f"{s.id}: {s.action} needs a target")
            if s.target and any(c.strategy == "coordinate" for c in s.target.candidates) and not s.review_required:
                errors.append(f"{s.id}: coordinate locator must be review_required")
        extracted = {e.output for e in self.extract}
        for e in self.extract:
            if e.after_step not in ids:
                errors.append(f"extractor {e.output}: unknown step {e.after_step}")
        missing = outputs - {o.split(".")[0] for o in extracted}
        if missing:
            errors.append(f"declared outputs without extractors: {sorted(missing)}")
        if not self.success.checks:
            errors.append("success condition needs at least one check")
        if errors:
            raise ValueError("; ".join(errors))
        return self


def _value_refs(step: Step) -> list[ValueRef]:
    refs = [step.value] if step.value else []
    for cond in [step.precondition, step.postcondition]:
        if cond is not None:
            refs.extend(_cond_refs(cond))
    return refs


def _cond_refs(cond: Any) -> list[ValueRef]:
    out: list[ValueRef] = []
    if isinstance(cond, ValueRef):
        return [cond]
    if isinstance(cond, BaseModel):
        for name in type(cond).model_fields:
            out.extend(_cond_refs(getattr(cond, name)))
    elif isinstance(cond, list):
        for item in cond:
            out.extend(_cond_refs(item))
    elif isinstance(cond, dict):
        for item in cond.values():
            out.extend(_cond_refs(item))
    return out


def bump_version(old: CapabilityArtifact, new: CapabilityArtifact) -> str:
    """Semver rule: contract change -> major; flow change -> minor; metadata only -> patch."""
    major, minor, patch = (int(x) for x in old.capability.version.split("."))
    if old.contract != new.contract:
        return f"{major + 1}.0.0"
    if (old.steps, old.extract, old.success, old.policy) != (new.steps, new.extract, new.success, new.policy):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"
