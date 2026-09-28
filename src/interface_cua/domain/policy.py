"""Policy documents and decisions. Effective policy = global ∩ tenant ∩ artifact (narrow-only)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.actions import ActionKind, RiskClass


class PolicyLayer(BaseModel):
    """One layer. ``None`` means "this layer adds no constraint on that dimension"."""

    model_config = ConfigDict(extra="forbid")

    id: str
    allowed_origins: list[str] | None = None
    allowed_routes: list[str] | None = None
    denied_routes: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionKind] | None = None
    # How irreversible actions are treated in replay. Discovery always denies them.
    irreversible_in_replay: Literal["deny", "require_approval"] | None = None
    keep_query_params: list[str] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["allow", "deny", "require_approval"]
    risk: RiskClass
    reasons: list[str]
    layer: str | None = None  # layer that denied, if any
    rule_ids: list[str] = Field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"
