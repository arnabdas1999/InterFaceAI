"""Policy engine. Evaluated before every discovery action, replay action, recovery-handler action,
dialog response, human remote-control action, and request/navigation.

Effective policy is the intersection of all layers (global ∩ tenant ∩ artifact): every layer must
allow; an artifact can only narrow.
"""

from __future__ import annotations

from typing import Literal

from interface_cua.domain.actions import ActionKind, BoundAction, RiskClass
from interface_cua.domain.policy import PolicyDecision, PolicyLayer
from interface_cua.domain.profiles import AppProfile
from interface_cua.domain.routes import canonicalize, route_allowed
from interface_cua.policy.risk import classify

Mode = Literal["discovery", "replay", "validation", "human", "system"]


class PolicyEngine:
    def __init__(self, layers: list[PolicyLayer], profile: AppProfile) -> None:
        if not layers:
            raise ValueError("at least one policy layer is required")
        self.layers = layers
        self.profile = profile

    # --- location checks ---------------------------------------------------------
    def location_allowed(self, origin: str, route: str) -> tuple[bool, str | None, str]:
        for layer in self.layers:
            if layer.allowed_origins is not None and origin not in layer.allowed_origins:
                return False, layer.id, f"origin {origin} not allowlisted"
            if route_allowed(route, layer.denied_routes):
                return False, layer.id, f"route {route} is denied"
            if layer.allowed_routes is not None and not route_allowed(route, layer.allowed_routes):
                return False, layer.id, f"route {route} not allowlisted"
        return True, None, "ok"

    def url_allowed(self, url: str) -> tuple[bool, str]:
        if url.startswith(("about:blank", "data:", "chrome-error:")):
            return True, "ok"
        cu = canonicalize(url)
        ok, _, reason = self.location_allowed(cu.origin, cu.path)
        return ok, reason

    def action_kind_allowed(self, kind: ActionKind) -> tuple[bool, str | None]:
        for layer in self.layers:
            if layer.allowed_actions is not None and kind not in layer.allowed_actions:
                return False, layer.id
        return True, None

    def irreversible_mode(self) -> Literal["deny", "require_approval"]:
        modes = [layer.irreversible_in_replay for layer in self.layers if layer.irreversible_in_replay]
        return "deny" if "deny" in modes or not modes else "require_approval"

    # --- action decision -----------------------------------------------------------
    def evaluate(
        self,
        action: BoundAction,
        *,
        mode: Mode,
        current_origin: str,
        current_route: str,
        dialog_text: str | None = None,
        dialog_known_safe: bool = False,
    ) -> PolicyDecision:
        risk, rule_ids, reasons = classify(
            action,
            self.profile,
            current_route=current_route,
            dialog_text=dialog_text,
            dialog_known_safe=dialog_known_safe,
        )

        def deny(reason: str, layer: str | None = None) -> PolicyDecision:
            return PolicyDecision(verdict="deny", risk=risk, reasons=[reason, *reasons], layer=layer, rule_ids=rule_ids)

        ok, layer = self.action_kind_allowed(action.kind)
        if not ok:
            return deny(f"action {action.kind.value} not allowed", layer)

        # The page we are acting on must itself be inside the allowlist.
        if action.kind != ActionKind.NAVIGATE:
            ok, layer, why = self.location_allowed(current_origin, current_route)
            if not ok:
                return deny(f"current page outside allowlist: {why}", layer)

        if action.kind == ActionKind.NAVIGATE:
            route = self.profile.route_handles.get(action.route_handle or "")
            if route is None:
                return deny(f"unknown route handle {action.route_handle!r}; free URLs are not expressible")
            ok, layer, why = self.location_allowed(current_origin, route)
            if not ok:
                return deny(why, layer)

        if action.href_route is not None:
            ok, layer, why = self.location_allowed(action.href_origin or current_origin, action.href_route)
            if not ok:
                return deny(f"link target outside allowlist: {why}", layer)

        if action.submits_form and action.form_action_route is not None:
            ok, layer, why = self.location_allowed(current_origin, action.form_action_route)
            if not ok:
                return deny(f"form target outside allowlist: {why}", layer)

        if action.key is not None and action.kind == ActionKind.PRESS:
            from interface_cua.domain.actions import ALLOWED_KEYS

            if action.key not in ALLOWED_KEYS:
                return deny(f"key {action.key!r} not in allowed key list")

        if risk == RiskClass.IRREVERSIBLE:
            if mode == "discovery":
                return deny("irreversible actions are blocked during discovery")
            if mode == "validation":
                return deny("validation never performs irreversible actions")
            if mode == "human":
                return PolicyDecision(
                    verdict="allow",
                    risk=risk,
                    reasons=["performed by the human operator in control", *reasons],
                    rule_ids=rule_ids,
                )
            if mode == "system":
                return deny("recovery handlers may not perform irreversible actions")
            if self.irreversible_mode() == "deny":
                return deny("irreversible actions are denied by policy")
            return PolicyDecision(
                verdict="require_approval",
                risk=risk,
                reasons=["irreversible action requires a bound human approval", *reasons],
                rule_ids=rule_ids,
            )

        return PolicyDecision(verdict="allow", risk=risk, reasons=reasons or ["within allowlist"], rule_ids=rule_ids)
