"""The enumerated action vocabulary shared by discovery, replay, and human remote control."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.conditions import ValueRef
from interface_cua.domain.targets import TargetSpec


class ActionKind(StrEnum):
    NAVIGATE = "navigate"  # to a route handle from the allowlist, never a free URL
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    DIALOG_RESPOND = "dialog_respond"


class RiskClass(StrEnum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    SENSITIVE = "sensitive_reversible"
    IRREVERSIBLE = "irreversible"

    @property
    def rank(self) -> int:
        return list(RiskClass).index(self)

    @staticmethod
    def max(*classes: RiskClass) -> RiskClass:
        return max(classes, key=lambda c: c.rank)


ALLOWED_KEYS = frozenset({"Enter", "Tab", "Escape"})


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float


class BoundAction(BaseModel):
    """A concrete action about to be performed on a resolved target. This is what policy judges.

    ``value`` is the resolved value (already substituted from inputs by the runner, never by the
    model); it is redacted before it is logged.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    target: TargetSpec | None = None
    value_ref: ValueRef | None = None
    value: str | None = Field(default=None, repr=False)
    select_by: Literal["value", "label"] | None = None
    key: str | None = None
    route_handle: str | None = None
    dialog_response: Literal["accept", "dismiss"] | None = None
    point: Point | None = None
    # Facts about the resolved element that policy uses for target-level risk rules.
    element_role: str | None = None
    element_name: str | None = None
    submits_form: bool = False
    form_method: str | None = None
    form_action_route: str | None = None
    href_route: str | None = None
    href_origin: str | None = None
    declared_intent_risk: RiskClass | None = None

    def fingerprint(self) -> str:
        parts = [
            self.kind.value,
            self.element_role or "",
            self.element_name or "",
            self.form_action_route or "",
            self.href_route or "",
            self.route_handle or "",
            self.key or "",
            self.dialog_response or "",
        ]
        return "|".join(parts)
