import pickle
import time

import pytest

from interface_cua.config import load_settings
from interface_cua.domain.actions import ActionKind, BoundAction, RiskClass
from interface_cua.domain.interventions import ControlState
from interface_cua.domain.policy import PolicyLayer
from interface_cua.handoff.control_lease import ControlLease, LeaseConflict, StaleLeaseError
from interface_cua.policy.engine import PolicyEngine
from interface_cua.policy.redaction import Redactor
from interface_cua.profiles.store import ProfileStore
from interface_cua.secrets.provider import SecretValue

pytestmark = pytest.mark.unit
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture(scope="module")
def profile():  # type: ignore[no-untyped-def]
    return ProfileStore(load_settings()).app_profile("synthcore@1.0.0")


@pytest.fixture(scope="module")
def engine(profile) -> PolicyEngine:  # type: ignore[no-untyped-def]
    store = ProfileStore(load_settings())
    return PolicyEngine([store.global_policy(), store.tenant_policy(store.tenant("tenant-a"))], profile)


def click(**kw: object) -> BoundAction:
    return BoundAction(kind=ActionKind.CLICK, element_role="button", **kw)  # type: ignore[arg-type]


def test_allowlist_blocks_admin_offsite_and_unknown_routes(engine: PolicyEngine) -> None:
    assert not engine.url_allowed(f"{ORIGIN}/__admin/faults")[0]
    assert not engine.url_allowed("https://example.org/")[0]
    assert not engine.url_allowed(f"{ORIGIN}/logout")[0]
    assert engine.url_allowed(f"{ORIGIN}/members/12345/accounts")[0]
    d = engine.evaluate(
        click(element_name="x", href_route="/promo", href_origin="https://example.com"),
        mode="replay",
        current_origin=ORIGIN,
        current_route="/members/12345",
    )
    assert d.verdict == "deny"


def test_artifact_layer_can_only_narrow(profile) -> None:  # type: ignore[no-untyped-def]
    store = ProfileStore(load_settings())
    base = [store.global_policy(), store.tenant_policy(store.tenant("tenant-a"))]
    widening = PolicyLayer(id="artifact", allowed_routes=["/**", "/__admin/**"], allowed_actions=list(ActionKind))
    eng = PolicyEngine([*base, widening], profile)
    assert not eng.url_allowed(f"{ORIGIN}/__admin/reset")[0]  # still denied by the global layer
    narrowing = PolicyLayer(id="artifact", allowed_routes=["/members/search"], allowed_actions=[ActionKind.CLICK])
    eng = PolicyEngine([*base, narrowing], profile)
    assert not eng.url_allowed(f"{ORIGIN}/members/12345")[0]
    assert (
        eng.evaluate(BoundAction(kind=ActionKind.TYPE), mode="replay", current_origin=ORIGIN, current_route="/members/search").verdict
        == "deny"
    )


def test_irreversible_is_blocked_in_discovery_and_approval_bound_in_replay(engine: PolicyEngine) -> None:
    commit = click(element_name="Open Account", submits_form=True, form_method="POST", form_action_route="/members/1/subaccounts/commit")
    kw = {"current_origin": ORIGIN, "current_route": "/members/1/subaccounts/review"}
    assert engine.evaluate(commit, mode="discovery", **kw).verdict == "deny"
    assert engine.evaluate(commit, mode="validation", **kw).verdict == "deny"
    assert engine.evaluate(commit, mode="system", **kw).verdict == "deny"
    replay = engine.evaluate(commit, mode="replay", **kw)
    assert replay.verdict == "require_approval" and replay.risk == RiskClass.IRREVERSIBLE
    assert "open-sub-account-commit" in replay.rule_ids


def test_unreviewed_post_submit_defaults_to_irreversible(engine: PolicyEngine) -> None:
    d = engine.evaluate(
        click(element_name="Save", submits_form=True, form_method="POST", form_action_route="/members/1/notes"),
        mode="replay",
        current_origin=ORIGIN,
        current_route="/members/1",
    )
    assert d.risk == RiskClass.IRREVERSIBLE and "default:unreviewed-post-submit" in d.rule_ids
    safe = engine.evaluate(
        click(element_name="Continue", submits_form=True, form_method="POST", form_action_route="/members/1/subaccounts/review"),
        mode="replay",
        current_origin=ORIGIN,
        current_route="/members/1/subaccounts/new",
    )
    assert safe.verdict == "allow"


def test_injected_close_account_is_denied_in_discovery(engine: PolicyEngine) -> None:
    d = engine.evaluate(
        click(element_name="Close Account", submits_form=True, form_method="POST", form_action_route="/members/24680/close"),
        mode="discovery",
        current_origin=ORIGIN,
        current_route="/members/24680",
    )
    assert d.verdict == "deny" and "close-account" in d.rule_ids


def test_declared_intent_only_raises_risk(engine: PolicyEngine) -> None:
    d = engine.evaluate(
        click(element_name="Search", declared_intent_risk=RiskClass.IRREVERSIBLE),
        mode="discovery",
        current_origin=ORIGIN,
        current_route="/members/search",
    )
    assert d.verdict == "deny"
    d = engine.evaluate(
        click(element_name="Close Account", declared_intent_risk=RiskClass.READ_ONLY),
        mode="discovery",
        current_origin=ORIGIN,
        current_route="/members/1",
    )
    assert d.risk == RiskClass.IRREVERSIBLE


def test_unknown_dialog_accept_is_irreversible(engine: PolicyEngine) -> None:
    accept = BoundAction(kind=ActionKind.DIALOG_RESPOND, dialog_response="accept")
    assert engine.evaluate(accept, mode="system", current_origin=ORIGIN, current_route="/members/1").verdict == "deny"
    assert (
        engine.evaluate(accept, mode="system", current_origin=ORIGIN, current_route="/members/1", dialog_known_safe=True).verdict == "allow"
    )


def test_disallowed_key_and_free_navigation(engine: PolicyEngine) -> None:
    assert (
        engine.evaluate(
            BoundAction(kind=ActionKind.PRESS, key="F5"), mode="replay", current_origin=ORIGIN, current_route="/members/search"
        ).verdict
        == "deny"
    )
    assert (
        engine.evaluate(
            BoundAction(kind=ActionKind.NAVIGATE, route_handle="http://evil"), mode="replay", current_origin=ORIGIN, current_route="/main"
        ).verdict
        == "deny"
    )


def test_redaction_model_egress_vs_persisted(profile) -> None:  # type: ignore[no-untyped-def]
    r = Redactor(b"k", sensitive_map=profile.sensitive_fields, inputs={"member_id": ("12345", "pii_low")}, secrets=["hunter2pass"])
    r.add_value("Avery Quinn", "name")
    text = "Member 12345 Avery Quinn balance $1,234.56 acct 8830012345 pwd hunter2pass Bearer abc.def a@b.co /main;jsessionid=ff00aa"
    model = r.for_model(text)
    for leaked in ["12345 ", "Avery", "1,234.56", "8830012345", "hunter2pass", "abc.def", "a@b.co", "ff00aa"]:
        assert leaked not in model, leaked
    assert "⟦input:member_id⟧" in model and "⟦masked:money⟧" in model
    persisted = r.text(text)
    assert "⟦input:member_id#" in persisted and "12345 " not in persisted
    assert r.obj({"password": "x", "cookie": "y", "nested": {"authorization": "z", "v": "12345"}}) == {
        "password": "⟦redacted⟧",
        "cookie": "⟦redacted⟧",
        "nested": {"authorization": "⟦redacted⟧", "v": f"⟦input:member_id#{r.hash8('12345')}⟧"},
    }
    assert r.output_value({"amount": "1234.56", "currency": "USD"}, "financial")["currency"] == "USD"
    assert "1234.56" not in str(r.output_value({"amount": "1234.56", "currency": "USD"}, "financial"))


def test_secret_value_cannot_leak() -> None:
    s = SecretValue("synthetic-only-pass")
    assert "synthetic" not in repr(s) and "synthetic" not in str(s) and f"{s}" == "SecretValue(***)"
    with pytest.raises(TypeError):
        pickle.dumps(s)


def test_lease_fencing_and_cas() -> None:
    lease = ControlLease("sess")
    auto_token = lease.version
    lease.check(auto_token, "automation")
    lease.transition(ControlState.PAUSING, expect_version=lease.version, owner="automation", reason="t")
    lease.transition(ControlState.HUMAN_PENDING, expect_version=lease.version, owner="unassigned", reason="t")
    with pytest.raises(StaleLeaseError):
        lease.check(auto_token, "automation")  # automation's token died when control was ceded
    v = lease.version
    lease.transition(ControlState.HUMAN_ACTIVE, expect_version=v, owner="human:a", reason="claim", expires_in_s=60)
    with pytest.raises(LeaseConflict):  # a concurrent second claim with the same expected version loses
        lease.transition(ControlState.HUMAN_ACTIVE, expect_version=v, owner="human:b", reason="claim")
    human_token = lease.version
    lease.check(human_token, "human:a")
    with pytest.raises(StaleLeaseError):
        lease.check(human_token, "automation")
    with pytest.raises(StaleLeaseError):
        lease.check(human_token, "human:b")
    with pytest.raises(LeaseConflict):
        lease.transition(ControlState.PAUSING, expect_version=lease.version, owner="automation", reason="illegal")


def test_lease_expiry() -> None:
    lease = ControlLease("sess")
    lease.transition(ControlState.PAUSING, expect_version=lease.version, owner="automation", reason="t")
    lease.transition(ControlState.HUMAN_PENDING, expect_version=lease.version, owner="unassigned", reason="t")
    lease.transition(ControlState.HUMAN_ACTIVE, expect_version=lease.version, owner="human:a", reason="t", expires_in_s=0.01)
    time.sleep(0.05)
    with pytest.raises(StaleLeaseError, match="expired"):
        lease.check(lease.version, "human:a")
