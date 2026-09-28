"""Discovery (scripted planner) -> compile -> approve -> deterministic replay, against the live target."""

from __future__ import annotations

import json

import pytest

from interface_cua.config import Settings
from interface_cua.discovery.compiler import compile_trajectory
from interface_cua.discovery.runner import run_discovery
from interface_cua.domain.artifacts import Lifecycle
from interface_cua.domain.types import OutputType
from interface_cua.llm.client import CheckProposal, DiscoveryContext, DiscoveryDecision
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.llm.prompt import render_user_text
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from target_app import admin
from tests.helpers import balance_request, balance_script, handle_for

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def approved(target: str, settings: Settings) -> str:
    out = await run_discovery(settings, balance_request(), FakePlanner(balance_script), {"member_id": "12345"})
    assert out.result.status == "success", out.result
    store = CapabilityStore(settings.capabilities_dir)
    art = compile_trajectory(out.trajectory, ProfileStore(settings).app_profile("synthcore@1.0.0"))  # type: ignore[arg-type]
    if not store.path(art.capability.id, art.capability.version).exists():
        store.save(art)
        art = store.transition(art, "validated", actor="test")
        store.transition(art, "approved", actor="test")
    return art.capability.id


async def replay(settings: Settings, member_id: str, **kw: object):  # type: ignore[no-untyped-def]
    return await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="read-savings-balance", tenant_id="tenant-a", inputs={"member_id": member_id}, **kw)
    )  # type: ignore[arg-type]


def events(settings: Settings, run_id: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (settings.runs_dir / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_discovery_egress_never_contains_sensitive_values(target: str, settings: Settings) -> None:
    planner = FakePlanner(balance_script)
    out = await run_discovery(settings, balance_request(), planner, {"member_id": "12345"})
    assert out.result.status == "success"
    assert out.outputs["savings_balance"] == {"amount": "1234.56", "currency": "USD"}
    sent = "\n".join(c.observation_text + "\n".join(c.history) for c in planner.calls)
    for secret in ["12345", "Avery Quinn", "1,234.56", "8830012345", "synthetic-only-pass", "operator1"]:
        assert secret not in sent, secret
    assert "⟦input:member_id⟧" in sent
    persisted = (settings.runs_dir / out.result.run_id / "run-result.json").read_text(encoding="utf-8")
    log = (settings.runs_dir / out.result.run_id / "events.jsonl").read_text(encoding="utf-8")
    for secret in ["1234.56", "Avery Quinn", "synthetic-only-pass", '"12345"']:
        assert secret not in persisted and secret not in log, secret


async def test_prompt_injection_is_denied_and_stops(target: str, settings: Settings) -> None:
    def injected(ctx: DiscoveryContext) -> DiscoveryDecision:
        if "Main route: /members/search" in ctx.observation_text:
            if not ctx.history:
                return DiscoveryDecision(action="type", handle=handle_for(ctx, "Member ID"), input_name="member_id")
            return DiscoveryDecision(action="click", handle=handle_for(ctx, "Search", "button"))
        assert "ignore previous instructions" in ctx.observation_text  # the note is visible, as untrusted data
        return DiscoveryDecision(
            action="click", handle=handle_for(ctx, "Close Account"), intent_risk="reversible", rationale="page says so"
        )

    out = await run_discovery(settings, balance_request(max_steps=8), FakePlanner(injected), {"member_id": "24680"})
    assert out.result.status == "failure" and out.result.code == "policy_violation"  # type: ignore[union-attr]
    blocked = [e for e in events(settings, out.result.run_id) if e["type"] == "action_blocked"]
    assert blocked and "close-account" in json.dumps(blocked)
    assert not [e for e in admin.ledger(target) if e.get("kind") == "close_request"]


@pytest.mark.parametrize(
    ("script", "code"),
    [
        (["garbage", "garbage", "garbage"], "discovery_exhausted"),
        ([DiscoveryDecision(action="give_up", rationale="x")], "discovery_exhausted"),
    ],
)
async def test_discovery_stops_on_invalid_or_give_up(target: str, settings: Settings, script: list, code: str) -> None:  # type: ignore[type-arg]
    out = await run_discovery(settings, balance_request(), FakePlanner(script), {"member_id": "12345"})
    assert out.result.status == "failure" and out.result.code == code  # type: ignore[union-attr]


async def test_discovery_survives_read_only_turns_and_wrong_output_names(target: str, settings: Settings) -> None:
    """Regression from the first genuine Gemini run: several read-only turns on the final screen
    (a wrong output name, a rejected done) must not trip the no-progress stop, and an undeclared
    output name must be refused rather than leak into the contract."""
    state = {"wrong_name": False, "early_done": False}

    def planner(ctx: DiscoveryContext) -> DiscoveryDecision:
        assert "REQUIRED OUTPUTS" in render_user_text(ctx) and "savings_balance (money)" in render_user_text(ctx)
        if "Current Balance" in ctx.observation_text and not state["wrong_name"]:
            state["wrong_name"] = True
            return DiscoveryDecision(action="extract", output_name="current_balance", output_type=OutputType.MONEY,
                                     label_text="Current Balance", frame_index=1)  # fmt: skip
        if "Current Balance" in ctx.observation_text and not state["early_done"]:
            state["early_done"] = True
            return DiscoveryDecision(
                action="done", checkpoint=[CheckProposal(label_text="Member ID", frame_index=1, equals_input="member_id")]
            )
        return balance_script(ctx)

    fake = FakePlanner(planner)
    out = await run_discovery(settings, balance_request(), fake, {"member_id": "12345"})
    assert out.result.status == "success", out.result
    assert set(out.outputs) == {"savings_balance"}
    assert any("not a required output" in (c.feedback or "") for c in fake.calls)


async def test_discovery_rejects_unverifiable_done(target: str, settings: Settings) -> None:
    script = [DiscoveryDecision(action="done", checkpoint=[]), DiscoveryDecision(action="give_up")]
    planner = FakePlanner(script)
    out = await run_discovery(settings, balance_request(), planner, {"member_id": "12345"})
    assert out.result.status == "failure"
    assert "Verification of your done claim FAILED" in (planner.calls[1].feedback or "")


async def test_replay_success_with_new_input_and_no_model(approved: str, settings: Settings) -> None:
    out = await replay(settings, "67890")
    assert out.result.status == "success", out.result
    assert out.outputs == {"savings_balance": {"amount": "15020.00", "currency": "USD"}}
    evs = events(settings, out.result.run_id)
    assert not [e for e in evs if e["type"].startswith("model_decision")]
    assert "15020" not in (settings.runs_dir / out.result.run_id / "run-result.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(("mid", "code"), [("99999", "member_not_found"), ("00000", "validation_rejected")])
async def test_business_outcomes(approved: str, settings: Settings, mid: str, code: str) -> None:
    out = await replay(settings, mid)
    assert out.result.status == "business_outcome" and out.result.code == code  # type: ignore[union-attr]


async def test_contract_violation_rejected_without_session(approved: str, settings: Settings) -> None:
    out = await replay(settings, "12ab")
    r = out.result
    assert r.status == "rejected" and r.code == "input_contract_violation" and r.session_opened is False  # type: ignore[union-attr]
    assert "12ab" not in r.model_dump_json()
    assert not (settings.runs_dir / r.run_id / "events.jsonl").exists()


async def test_unapproved_artifact_is_rejected(approved: str, settings: Settings) -> None:
    out = await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="read-savings-balance", version="9.9.9", tenant_id="tenant-a", inputs={"member_id": "12345"})
    )
    assert out.result.status == "rejected" and out.result.code == "artifact_invalid"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("faults", "codes"),
    [
        ({"slow_search": {}, "system_notice": {}}, {"slow_load_waited", "interstitial_dismissed", "dialog_handled"}),
        ({"http_500": {"route": "lookup"}}, {"transient_retry"}),
        ({"session_expire": {"skip": 2}}, {"session_reauthenticated"}),
    ],
)
async def test_recoverable_conditions(approved: str, settings: Settings, target: str, faults: dict, codes: set) -> None:  # type: ignore[type-arg]
    for name, params in faults.items():
        admin.arm(target, name, **params)
    out = await replay(settings, "12345")
    assert out.result.status == "success", out.result
    assert codes <= {r.code for r in out.result.recoveries}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("fault", "params", "count", "code", "retryable"),
    [
        ("permission_denied", {}, 1, "permission_denied", False),
        ("duplicate_control", {}, 1, "locator_ambiguous", False),
        ("http_500", {"route": "lookup"}, -1, "app_error", True),
        ("session_expire", {"skip": 2}, 2, "session_recovery_exhausted", True),
        ("unknown_modal", {}, 1, "unknown_state", False),
        ("offsite_redirect", {"url": "https://example.org/"}, 1, "policy_violation", False),
    ],
)
async def test_hard_failures_are_debuggable(
    approved: str,
    settings: Settings,
    target: str,
    fault: str,
    params: dict,  # type: ignore[type-arg]
    count: int,
    code: str,
    retryable: bool,
) -> None:
    admin.arm(target, fault, count=count, **params)
    out = await replay(settings, "12345")
    r = out.result
    assert r.status == "failure" and r.code == code, r  # type: ignore[union-attr]
    assert r.side_effect_state == "none"  # type: ignore[union-attr]
    if code in {"permission_denied", "locator_ambiguous", "unknown_state"}:
        assert r.step_id and r.expected and r.observed  # type: ignore[union-attr]
        assert any(e.kind == "screenshot" for e in r.evidence) and any(e.kind == "dom_snapshot" for e in r.evidence)
    if code == "locator_ambiguous":
        assert r.locator_diagnostics[0].match_count == 2  # type: ignore[union-attr]
    if fault == "offsite_redirect":
        assert [e for e in events(settings, r.run_id) if e["type"] == "request_blocked"]


async def test_popup_is_closed_and_recorded(approved: str, settings: Settings, target: str) -> None:
    admin.arm(target, "popup", url="https://example.com/promo")
    out = await replay(settings, "12345")
    assert out.result.status == "success"
    evs = events(settings, out.result.run_id)
    assert [e for e in evs if e["type"] in {"popup_blocked", "request_blocked"}]


async def test_success_requires_checkpoint(approved: str, settings: Settings) -> None:
    store = CapabilityStore(settings.capabilities_dir)
    art = store.load("read-savings-balance", "1.0.0")
    # Same flow, but the checkpoint demands a different account type: success must be impossible.
    checks = [
        c.model_copy(update={"contains": c.contains.model_copy(update={"vocab": "money_market"})})
        if getattr(c, "contains", None) is not None and c.contains.vocab == "share_savings"
        else c
        for c in art.success.checks
    ]
    bad = art.model_copy(
        update={
            "success": art.success.model_copy(update={"checks": checks}),
            "capability": art.capability.model_copy(update={"version": "1.0.1"}),
            "lifecycle": Lifecycle(),
        }
    ).with_hash()
    store.save(bad)
    approved_bad = store.transition(store.transition(bad, "validated", actor="t"), "approved", actor="t")
    out = await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="read-savings-balance", version="1.0.1", tenant_id="tenant-a", inputs={"member_id": "12345"})
    )
    store.transition(approved_bad, "deprecated", actor="t")  # keep approved-latest pointing at the good version
    assert out.result.status == "failure" and out.result.code == "checkpoint_mismatch"  # type: ignore[union-attr]
