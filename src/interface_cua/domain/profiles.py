"""App profile (per vendor product) and tenant profile.

The app profile is reviewed once per vendor product and shared by every capability and every
tenant running that product. It carries what one happy-path discovery run cannot teach: the state
catalog (business outcomes, recoverable conditions, hard failures), risk rules, the sensitive-field
map, the login routine, and fingerprint rules.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.actions import RiskClass
from interface_cua.domain.conditions import Condition
from interface_cua.domain.targets import RegionSpec, TargetSpec


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Handler(_Strict):
    """A bounded, named, deterministic response to a known state."""

    kind: Literal["dismiss", "dialog", "reauthenticate", "retry_step", "wait"]
    target: TargetSpec | None = None
    dialog_response: Literal["accept", "dismiss"] | None = None
    max_attempts: int = 1
    backoff_ms: int = 500
    wait_until: Condition | None = None
    wait_timeout_ms: int = 10_000


class CatalogState(_Strict):
    id: str
    description: str
    detector: Condition
    classification: Literal["business_outcome", "recoverable", "hard_failure"]
    # global: checked before/after every step; step: only where a capability maps it.
    scope: Literal["global", "step"]
    handler: Handler | None = None
    recovery_code: str | None = None  # e.g. interstitial_dismissed (recorded, not an error)
    error_code: str | None = None  # for hard failures, e.g. permission_denied
    retryable: bool = False
    message_region: RegionSpec | None = None  # sanitized message is returned with outcomes/failures
    default_outcome_code: str | None = None  # suggestion the compiler maps to a capability outcome
    # Step-scoped states: attach to recorded steps that submit a form on one of these routes.
    applies_after_submit_on: list[str] = Field(default_factory=list)


class LoginStep(_Strict):
    action: Literal["type", "click"]
    target: TargetSpec
    secret_field: Literal["username", "password"] | None = None


class LoginRoutine(_Strict):
    route: str
    secret_ref: str
    steps: list[LoginStep]
    success: Condition
    session_valid: Condition  # cheap check that the operator session is still alive


class SensitiveLabel(_Strict):
    label_pattern: str  # regex on the label cell text; the adjacent value cell is sensitive
    kind: str  # name | account_number | money | dob | ssn | address | phone | member_id


class SensitivePattern(_Strict):
    pattern: str
    kind: str


class SensitiveFieldMap(_Strict):
    labels: list[SensitiveLabel] = Field(default_factory=list)
    patterns: list[SensitivePattern] = Field(default_factory=list)


class RiskRuleMatch(_Strict):
    route: str | None = None
    role: str | None = None
    name_pattern: str | None = None  # regex on accessible/inferred name (vocab-resolved)
    form_action_route: str | None = None
    dialog_text_pattern: str | None = None


class RiskRule(_Strict):
    id: str
    match: RiskRuleMatch
    risk: RiskClass
    reason: str


class Fingerprint(_Strict):
    product: str
    generator_pattern: str  # regex on <meta name="generator">
    version_range: str  # e.g. ">=4.2,<5.0"
    required_anchor_texts: list[str] = Field(default_factory=list)


class AppProfile(_Strict):
    id: str
    version: str
    vendor_product: str
    description: str
    fingerprint: Fingerprint
    route_handles: dict[str, str]
    login: LoginRoutine | None = None  # None: the surface has no sign-on (e.g. a desktop client run as the service user)
    state_catalog: list[CatalogState]
    risk_rules: list[RiskRule]
    safe_post_routes: list[str]
    sensitive_fields: SensitiveFieldMap
    vocabulary: dict[str, str]

    def state(self, state_id: str) -> CatalogState:
        for s in self.state_catalog:
            if s.id == state_id:
                return s
        raise KeyError(state_id)


class TenantProfile(_Strict):
    tenant_id: str
    display_name: str
    app_profile: str  # "synthcore@1.0.0"
    base_url: str
    product_version: str
    vocabulary: dict[str, str] = Field(default_factory=dict)
    secret_ref: str
    policy_file: str | None = None
    override_refs: list[str] = Field(default_factory=list)
    data_classification: Literal["synthetic", "sandbox", "production"] = "production"
    # How this tenant's deployment is driven: the same artifact, a different adapter.
    surface: Literal["web", "legacy-web", "desktop"] = "web"
    root_frame: str | None = None  # legacy-web: the frame that hosts the application inside a frameset shell
    desktop_launch: list[str] | None = None  # desktop: command that starts the client (deployment-specific)
    window_title: str | None = None  # desktop: window title prefix that identifies the client

    def vocab(self, profile: AppProfile) -> dict[str, str]:
        return {**profile.vocabulary, **self.vocabulary}
