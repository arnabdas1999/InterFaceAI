"""Target-level risk classification. Risk is judged per bound action on a specific control,
not per action type: "click" on "Search" and "click" on "Open Account" are different risks."""

from __future__ import annotations

import re

from interface_cua.domain.actions import ActionKind, BoundAction, RiskClass
from interface_cua.domain.profiles import AppProfile
from interface_cua.domain.routes import match_route, route_allowed

BASE_RISK: dict[ActionKind, RiskClass] = {
    ActionKind.EXTRACT: RiskClass.READ_ONLY,
    ActionKind.WAIT_FOR: RiskClass.READ_ONLY,
    ActionKind.NAVIGATE: RiskClass.REVERSIBLE,
    ActionKind.CLICK: RiskClass.REVERSIBLE,
    ActionKind.TYPE: RiskClass.REVERSIBLE,
    ActionKind.SELECT: RiskClass.REVERSIBLE,
    ActionKind.PRESS: RiskClass.REVERSIBLE,
    ActionKind.DIALOG_RESPOND: RiskClass.REVERSIBLE,
}


def classify(
    action: BoundAction,
    profile: AppProfile,
    *,
    current_route: str,
    dialog_text: str | None = None,
    dialog_known_safe: bool = False,
) -> tuple[RiskClass, list[str], list[str]]:
    """Return (risk, matched rule ids, reasons). Declared intent can only raise risk."""
    risk = BASE_RISK[action.kind]
    rule_ids: list[str] = []
    reasons: list[str] = []

    for rule in profile.risk_rules:
        m = rule.match
        if m.route and match_route(m.route, current_route) is None:
            continue
        if m.role and m.role != (action.element_role or ""):
            continue
        if m.name_pattern and not re.search(m.name_pattern, action.element_name or ""):
            continue
        if m.form_action_route and (
            not action.submits_form or not action.form_action_route or match_route(m.form_action_route, action.form_action_route) is None
        ):
            continue
        if m.dialog_text_pattern and not re.search(m.dialog_text_pattern, dialog_text or ""):
            continue
        # A route-only rule shouldn't make navigation-free reads risky.
        if action.kind in {ActionKind.EXTRACT, ActionKind.WAIT_FOR} and rule.risk != RiskClass.IRREVERSIBLE:
            continue
        risk = RiskClass.max(risk, rule.risk)
        rule_ids.append(rule.id)
        reasons.append(rule.reason)

    # Default-deny: a POST form submit on a route not reviewed as safe is treated as irreversible.
    is_post = action.submits_form and (action.form_method or "GET").upper() == "POST"
    if is_post and (not action.form_action_route or not route_allowed(action.form_action_route, profile.safe_post_routes)):
        risk = RiskClass.IRREVERSIBLE
        rule_ids.append("default:unreviewed-post-submit")
        reasons.append("Submits a POST form to a route not reviewed as safe.")

    if action.kind == ActionKind.DIALOG_RESPOND and action.dialog_response == "accept" and not dialog_known_safe:
        risk = RiskClass.IRREVERSIBLE
        rule_ids.append("default:unknown-dialog-accept")
        reasons.append("Accepting a dialog that is not catalogued as safe.")

    if action.declared_intent_risk is not None and action.declared_intent_risk.rank > risk.rank:
        risk = action.declared_intent_risk
        reasons.append("Raised by declared intent.")

    return risk, rule_ids, reasons
