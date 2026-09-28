"""Same-session human handoff, fenced lease, approvals, SLA expiry - against the live target."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from interface_cua.config import Limits, Settings
from interface_cua.discovery.compiler import AuthoredPatch, apply_patch, compile_trajectory
from interface_cua.discovery.runner import run_discovery
from interface_cua.handoff.manager import InterventionManager
from interface_cua.handoff.operator_api import OperatorServer
from interface_cua.handoff.scripted_operator import OperatorStep, ScriptedOperator
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from target_app import admin
from tests.helpers import TOKENS, balance_request, balance_script, operator_client, subaccount_request, subaccount_script

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]
SUB_INPUTS = {"member_id": "67890", "product": "share_certificate", "nickname": "CD Ladder"}


@pytest.fixture(scope="module")
async def artifacts(target: str, settings: Settings) -> None:
    store = CapabilityStore(settings.capabilities_dir)
    profile = ProfileStore(settings).app_profile("synthcore@1.0.0")
    if not store.path("read-savings-balance", "1.0.0").exists():
        out = await run_discovery(settings, balance_request(), FakePlanner(balance_script), {"member_id": "12345"})
        a = store.save(compile_trajectory(out.trajectory, profile)) and store.load("read-savings-balance", "1.0.0")  # type: ignore[arg-type]
        store.transition(store.transition(a, "validated", actor="t"), "approved", actor="t")
    out = await run_discovery(
        settings,
        subaccount_request(),
        FakePlanner(subaccount_script),
        {"member_id": "12345", "product": "money_market", "nickname": "Vacation Fund"},
    )
    assert out.result.status == "success", out.result
    v1 = compile_trajectory(out.trajectory, profile)  # type: ignore[arg-type]
    patch = AuthoredPatch.model_validate_json((ROOT / "apps/synthcore/authored/open-sub-account.commit.json").read_text(encoding="utf-8"))
    v2 = apply_patch(v1, patch, profile)
    store.save(v1)
    store.save(v2)
    store.transition(store.transition(v2, "validated", actor="t"), "approved", actor="t")


@pytest.fixture
async def handoff(settings: Settings) -> AsyncIterator[InterventionManager]:
    manager = InterventionManager(settings)
    server = OperatorServer(manager, settings.operator_port)
    await server.start()
    yield manager
    await server.stop()


def op_url(settings: Settings) -> str:
    return f"http://127.0.0.1:{settings.operator_port}"


def events(settings: Settings, run_id: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (settings.runs_dir / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_unknown_state_handoff_uses_the_same_live_session(
    artifacts: None, settings: Settings, target: str, handoff: InterventionManager
) -> None:
    admin.arm(target, "unknown_modal")
    op = ScriptedOperator(op_url(settings), "op-1", TOKENS["op-1"])
    task = asyncio.create_task(
        op.run(
            [
                OperatorStep("click_control", name="I have a permissible purpose", role="checkbox"),
                OperatorStep("click_control", name="Acknowledge", role="button"),
                OperatorStep("resolve", resolution="resume", note="attestation completed"),
            ]
        )
    )
    out = await ReplayEngine(settings, escalator=handoff).invoke(
        InvocationRequest(
            capability_id="read-savings-balance", tenant_id="tenant-a", inputs={"member_id": "12345"}, escalation_mode="escalate"
        )
    )
    await task
    r = out.result
    assert r.status == "success", r
    assert r.interventions[0].resolution == "resume" and r.interventions[0].human_actions == 2
    evs = events(settings, r.run_id)
    started = next(e for e in evs if e["type"] == "session_started")["data"]
    resolved = next(e for e in evs if e["type"] == "intervention_resolved")["data"]
    assert (
        resolved["same_session"] is True
        and resolved["session_id"] == started["session_id"]
        and resolved["context_id"] == started["context_id"]
    )
    # Automation performed no actions while a human owned the session.
    human_window = [e for e in evs if e.get("control_owner", "").startswith("human:")]
    assert human_window and all(e["type"] in {"human_action", "lease_transition"} for e in human_window)
    states = [e["data"]["to"] for e in evs if e["type"] == "lease_transition"]
    assert states == ["PAUSING", "HUMAN_PENDING", "HUMAN_ACTIVE", "RESUMING", "AUTOMATION_ACTIVE"]
    assert next(e for e in evs if e["type"] == "resume_point")["data"]["decision"]["kind"] == "retry"


async def test_stale_token_and_concurrent_claims_are_rejected(
    artifacts: None, settings: Settings, target: str, handoff: InterventionManager
) -> None:
    admin.arm(target, "unknown_modal")
    run = asyncio.create_task(
        ReplayEngine(settings, escalator=handoff).invoke(
            InvocationRequest(
                capability_id="read-savings-balance", tenant_id="tenant-a", inputs={"member_id": "12345"}, escalation_mode="escalate"
            )
        )
    )
    async with operator_client(op_url(settings)) as c:
        iv = await ScriptedOperator(op_url(settings), api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
        env = handoff.env
        assert env is not None
        with pytest.raises(PermissionError):  # automation's pre-pause token is dead
            await env.adapter.raw_click(1, 1, token=1, actor="automation")
        control = (await c.get("/runs/current/control")).json()
        assert control["owner"] == "unassigned" and control["expected_owner"].startswith("human")
        a, b = await asyncio.gather(
            c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "a", "expect_version": control["version"]}),
            c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "b", "expect_version": control["version"]}),
        )
        assert sorted([a.status_code, b.status_code]) == [200, 409]
        winner = a if a.status_code == 200 else b
        who = "a" if winner is a else "b"
        token = winner.json()["token"]
        bad = await c.post(
            f"/interventions/{iv['id']}/act",
            json={"operator_id": "a" if who == "b" else "b", "token": token, "kind": "press", "key": "Tab"},
        )
        assert bad.status_code == 409
        blocked = await c.post(f"/interventions/{iv['id']}/resolve", json={"operator_id": who, "kind": "abort", "token": token + 5})
        assert blocked.status_code == 409
        ok = await c.post(f"/interventions/{iv['id']}/resolve", json={"operator_id": who, "kind": "abort", "token": token, "note": "test"})
        assert ok.status_code == 200
    r = (await run).result
    assert r.status == "aborted" and r.code == "operator_abort"  # type: ignore[union-attr]


async def test_unclaimed_intervention_aborts_at_sla(artifacts: None, settings: Settings, target: str) -> None:
    fast = dataclasses.replace(settings, limits=Limits(claim_sla_s=1, human_active_max_s=5), operator_port=8797)
    manager = InterventionManager(fast)
    server = OperatorServer(manager, fast.operator_port)
    await server.start()
    try:
        admin.arm(target, "unknown_modal")
        out = await ReplayEngine(fast, escalator=manager).invoke(
            InvocationRequest(
                capability_id="read-savings-balance", tenant_id="tenant-a", inputs={"member_id": "12345"}, escalation_mode="escalate"
            )
        )
    finally:
        await server.stop()
    assert out.result.status == "aborted" and out.result.code == "intervention_timeout"  # type: ignore[union-attr]


async def test_validation_of_irreversible_capability_never_commits(artifacts: None, settings: Settings, target: str) -> None:
    out = await ReplayEngine(settings).invoke(
        InvocationRequest(
            capability_id="open-sub-account",
            version="2.0.0",
            tenant_id="tenant-a",
            inputs=SUB_INPUTS,
            mode="validation",
            allow_unapproved=True,
            stop_before_irreversible=True,
        )
    )
    assert out.result.status == "success" and out.result.validation_stop == "c1"  # type: ignore[union-attr]
    assert admin.ledger(target) == []


async def test_irreversible_requires_escalation_mode(artifacts: None, settings: Settings, target: str) -> None:
    out = await ReplayEngine(settings).invoke(InvocationRequest(capability_id="open-sub-account", tenant_id="tenant-a", inputs=SUB_INPUTS))
    assert out.result.status == "rejected" and out.result.code == "policy_denied_preflight"  # type: ignore[union-attr]
    assert admin.ledger(target) == []


@pytest.mark.parametrize(("decision", "status", "commits"), [("approve", "success", 1), ("deny", "failure", 0)])
async def test_approval_is_bound_and_single_use(
    artifacts: None, settings: Settings, target: str, handoff: InterventionManager, decision: str, status: str, commits: int
) -> None:
    op = ScriptedOperator(op_url(settings), "supervisor-1", TOKENS["supervisor-1"])
    task = asyncio.create_task(op.run([OperatorStep("resolve", resolution=decision, note="checked with member")]))
    out = await ReplayEngine(settings, escalator=handoff).invoke(
        InvocationRequest(capability_id="open-sub-account", tenant_id="tenant-a", inputs=SUB_INPUTS, escalation_mode="escalate")
    )
    await task
    r = out.result
    assert r.status == status, r
    assert len(admin.ledger(target)) == commits
    if decision == "approve":
        assert out.outputs["confirmation_number"].startswith("CNF-") and len(out.outputs["new_account_last4"]) == 4
    else:
        assert r.code == "approval_denied" and r.side_effect_state == "none"  # type: ignore[union-attr]


async def test_approval_with_wrong_hash_is_refused(artifacts: None, settings: Settings, target: str, handoff: InterventionManager) -> None:
    run = asyncio.create_task(
        ReplayEngine(settings, escalator=handoff).invoke(
            InvocationRequest(capability_id="open-sub-account", tenant_id="tenant-a", inputs=SUB_INPUTS, escalation_mode="escalate")
        )
    )
    async with operator_client(op_url(settings)) as c:
        iv = await ScriptedOperator(op_url(settings), api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
        wrong = await c.post(
            f"/interventions/{iv['id']}/resolve", json={"operator_id": "supervisor-1", "kind": "approve", "action_hash": "sha256:forged"}
        )
        assert wrong.status_code == 409
        await c.post(f"/interventions/{iv['id']}/resolve", json={"operator_id": "supervisor-1", "kind": "deny", "note": "forged attempt"})
    assert (await run).result.status == "failure"
    assert admin.ledger(target) == []
