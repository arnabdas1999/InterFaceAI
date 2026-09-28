"""Handoff paths that the core suite did not exercise: heartbeat loss, the human-active limit, direct
manipulation of the browser window, the operator console page itself, and escalation during discovery."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from playwright.async_api import async_playwright

from interface_cua.config import Limits, Settings
from interface_cua.discovery.compiler import compile_trajectory
from interface_cua.discovery.runner import run_discovery
from interface_cua.handoff.manager import InterventionManager
from interface_cua.handoff.operator_api import OperatorServer
from interface_cua.handoff.scripted_operator import OperatorStep, ScriptedOperator
from interface_cua.llm.client import DiscoveryContext, DiscoveryDecision
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from target_app import admin
from tests.helpers import TOKENS, auth, balance_request, balance_script, operator_client

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
async def balance_artifact(target: str, settings: Settings) -> None:
    store = CapabilityStore(settings.capabilities_dir)
    if store.path("read-savings-balance", "1.0.0").exists():
        return
    out = await run_discovery(settings, balance_request(), FakePlanner(balance_script), {"member_id": "12345"})
    art = compile_trajectory(out.trajectory, ProfileStore(settings).app_profile("synthcore@1.0.0"))  # type: ignore[arg-type]
    store.save(art)
    store.transition(store.transition(art, "validated", actor="t"), "approved", actor="t")


async def running_handoff(
    settings: Settings, port: int, limits: Limits | None = None
) -> tuple[Settings, InterventionManager, OperatorServer]:
    s = dataclasses.replace(settings, operator_port=port, limits=limits or settings.limits)
    manager = InterventionManager(s)
    server = OperatorServer(manager, port)
    await server.start()
    return s, manager, server


@pytest.fixture
async def handoff(settings: Settings) -> AsyncIterator[tuple[Settings, InterventionManager]]:
    s, manager, server = await running_handoff(settings, 8791)
    yield s, manager
    await server.stop()


def start_replay(s: Settings, manager: InterventionManager) -> asyncio.Task:  # type: ignore[type-arg]
    return asyncio.create_task(
        ReplayEngine(s, escalator=manager).invoke(
            InvocationRequest(
                capability_id="read-savings-balance", tenant_id="tenant-a", inputs={"member_id": "12345"}, escalation_mode="escalate"
            )
        )
    )


def events(s: Settings, run_id: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (s.runs_dir / run_id / "events.jsonl").read_text(encoding="utf-8").splitlines()]


async def test_lost_heartbeats_return_the_session_to_the_queue(balance_artifact: None, settings: Settings, target: str) -> None:
    s, manager, server = await running_handoff(settings, 8792, Limits(claim_sla_s=60, human_active_max_s=60, heartbeat_s=0.2))  # type: ignore[arg-type]
    try:
        admin.arm(target, "unknown_modal")
        run = start_replay(s, manager)
        base = f"http://127.0.0.1:{s.operator_port}"
        async with operator_client(base) as c:
            iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
            first = (await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "alice"})).json()
            await asyncio.sleep(1.5)  # alice sends no heartbeats; lease expires after 3 x 0.2 s
            control = (await c.get("/runs/current/control")).json()
            assert control["state"] == "HUMAN_PENDING" and control["owner"] == "unassigned"
            stale = await c.post(
                f"/interventions/{iv['id']}/act", json={"operator_id": "alice", "token": first["token"], "kind": "press", "key": "Tab"}
            )
            assert stale.status_code == 409  # alice's token died with her lease
            second = (await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "bob"})).json()
            await c.post(
                f"/interventions/{iv['id']}/resolve", json={"operator_id": "bob", "kind": "abort", "token": second["token"], "note": "t"}
            )
        result = (await run).result
        assert result.status == "aborted" and result.code == "operator_abort"  # type: ignore[union-attr]
        states = [e["data"]["to"] for e in events(s, result.run_id) if e["type"] == "lease_transition"]
        assert states == ["PAUSING", "HUMAN_PENDING", "HUMAN_ACTIVE", "HUMAN_PENDING", "HUMAN_ACTIVE", "ABORTED"]
    finally:
        await server.stop()


async def test_human_active_limit_aborts_the_run(balance_artifact: None, settings: Settings, target: str) -> None:
    s, manager, server = await running_handoff(settings, 8793, Limits(claim_sla_s=60, human_active_max_s=1, heartbeat_s=30))
    try:
        admin.arm(target, "unknown_modal")
        run = start_replay(s, manager)
        base = f"http://127.0.0.1:{s.operator_port}"
        async with operator_client(base) as c:
            iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
            await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "alice"})
        result = (await run).result
        assert result.status == "aborted" and result.code == "intervention_timeout"  # type: ignore[union-attr]
    finally:
        await server.stop()


async def test_direct_manipulation_of_the_browser_window_is_journaled(
    balance_artifact: None, target: str, handoff: tuple[Settings, InterventionManager]
) -> None:
    s, manager = handoff
    admin.arm(target, "unknown_modal")
    run = start_replay(s, manager)
    base = f"http://127.0.0.1:{s.operator_port}"
    async with operator_client(base) as c:
        iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
        token = (await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "alice"})).json()["token"]
        page = manager.env.adapter.page  # type: ignore[union-attr]
        # A person's own mouse on the headed window: no lease token involved, attribution comes from the lease owner.
        await page.click("#att1")
        await page.click("#ackbtn")
        await asyncio.sleep(0.3)
        await c.post(
            f"/interventions/{iv['id']}/resolve", json={"operator_id": "alice", "kind": "resume", "token": token, "note": "done by hand"}
        )
    result = (await run).result
    assert result.status == "success", result
    human = [e for e in events(s, result.run_id) if e["type"] == "human_action"]
    assert [h["data"]["action"]["source"] for h in human] and all(h["data"]["action"]["source"] == "headed_window" for h in human)
    names = [h["data"]["action"]["name"] for h in human]
    assert "Acknowledge" in names and any("permissible purpose" in n for n in names)
    assert all(h["control_owner"] == "human:alice" for h in human)


async def test_operator_console_page_drives_a_real_handoff(
    balance_artifact: None, target: str, handoff: tuple[Settings, InterventionManager]
) -> None:
    s, manager = handoff
    admin.arm(target, "unknown_modal")
    run = start_replay(s, manager)
    base = f"http://127.0.0.1:{s.operator_port}"
    async with operator_client(base) as c:
        iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        console = await browser.new_page()
        dialogs: list[str] = []
        console.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
        await console.goto(f"{base}/console/{iv['id']}")
        await console.fill("#api", TOKENS["carol"])
        await console.click("text=Sign in")
        await console.wait_for_function("document.getElementById('mode').textContent.includes('masked snapshot')")
        await console.click("text=Claim live session")
        await console.wait_for_function("document.getElementById('tok').textContent.startsWith('lease token')")
        await console.wait_for_function("document.getElementById('mode').textContent.startsWith('LIVE')")
        async with operator_client(base) as c:
            for name in ("I have a permissible purpose", "Acknowledge"):
                ctrl = next(x for x in (await c.get(f"/interventions/{iv['id']}/controls")).json() if x["name"] == name)
                await console.wait_for_function("document.getElementById('shot').naturalWidth > 0")
                natural, client = await console.evaluate("() => [shot.naturalWidth, shot.clientWidth]")
                scale = client / natural
                await console.click("#shot", position={"x": ctrl["x"] * scale, "y": ctrl["y"] * scale})
                await console.wait_for_timeout(600)  # the console refreshes the screenshot after each action
        await console.fill("#note", "handled from the console")
        await console.click("text=Hand back: resume")
        await console.wait_for_function("document.getElementById('mode').textContent.startsWith('handed back')")
        assert dialogs == []  # no blocking dialogs: a native prompt would freeze the heartbeat timer
        await browser.close()
    result = (await run).result
    assert result.status == "success", result
    human = [e for e in events(s, result.run_id) if e["type"] == "human_action"]
    assert {h["data"]["action"]["source"] for h in human} == {"remote_console"} and len(human) == 2
    assert result.interventions[0].operator_id == "carol"


async def test_discovery_escalation_turns_human_steps_into_reviewable_artifact_steps(target: str, settings: Settings) -> None:
    s, manager, server = await running_handoff(settings, 8794)
    try:
        admin.arm(target, "unknown_modal")

        def planner(ctx: DiscoveryContext) -> DiscoveryDecision:
            if "Compliance attestation required" in ctx.observation_text:
                return DiscoveryDecision(action="give_up", rationale="an unfamiliar attestation modal blocks the page")
            return balance_script(ctx)

        op = ScriptedOperator(f"http://127.0.0.1:{s.operator_port}", "dana", TOKENS["dana"])
        task = asyncio.create_task(
            op.run(
                [
                    OperatorStep("click_control", name="I have a permissible purpose", role="checkbox"),
                    OperatorStep("click_control", name="Acknowledge", role="button"),
                    OperatorStep("resolve", resolution="resume", note="attested"),
                ]
            )
        )
        out = await run_discovery(
            s, balance_request(escalation_mode="escalate"), FakePlanner(planner), {"member_id": "12345"}, escalator=manager
        )
        await task
    finally:
        await server.stop()
    assert out.result.status == "success", out.result
    assert out.result.interventions and out.result.interventions[0].kind == "discovery_stuck"
    art = compile_trajectory(out.trajectory, ProfileStore(s).app_profile("synthcore@1.0.0"))  # type: ignore[arg-type]
    human = [st for st in art.steps if st.provenance == "human"]
    assert len(human) == 2 and all(st.review_required for st in human)
    assert art.review.review_required and any("human operator" in item for item in art.review.items)
    assert "12345" not in art.model_dump_json()  # human-step routes were generalized too


async def test_operator_api_enforces_identity_roles_tenants_and_live_view_ownership(
    balance_artifact: None, target: str, handoff: tuple[Settings, InterventionManager]
) -> None:
    s, manager = handoff
    admin.arm(target, "unknown_modal")
    run = start_replay(s, manager)
    base = f"http://127.0.0.1:{s.operator_port}"
    async with operator_client(base) as c:
        iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
        path = f"/interventions/{iv['id']}"
        async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
            assert (await anon.get("/interventions")).status_code == 401
            assert (await anon.post(f"{path}/claim", json={})).status_code == 401
            assert (await anon.get(path, headers={"authorization": "Bearer forged"})).status_code == 401
            outsider = auth("outsider")  # tenant-b only
            assert (await anon.get("/interventions", headers=outsider)).json() == []
            assert (await anon.get(path, headers=outsider)).status_code == 403
            spoof = await anon.post(f"{path}/claim", json={"operator_id": "supervisor-1"}, headers=auth("alice"))
            assert spoof.status_code == 403  # identity comes from the token, not the body
        token = (await c.post(f"{path}/claim", json={"operator_id": "alice"})).json()["token"]
        not_owner = await c.get(f"{path}/stream", params={"token": token}, headers=auth("bob"))
        assert not_owner.status_code == 403
        async with c.stream("GET", f"{path}/stream", params={"token": token}, headers=auth("alice")) as live:
            assert live.status_code == 200
            await manager.env.adapter.page.mouse.move(5, 5)  # type: ignore[union-attr]  # any repaint yields a frame
            chunk = await asyncio.wait_for(anext(live.aiter_bytes()), timeout=10)
            assert chunk.startswith(b"data: /9j/")  # a JPEG frame, streamed only to the lease owner
        plain = await c.post(f"{path}/resolve", json={"operator_id": "alice", "kind": "approve", "action_hash": "x"})
        assert plain.status_code == 403  # approving is a supervisor decision
        await c.post(f"{path}/resolve", json={"operator_id": "alice", "kind": "abort", "token": token, "note": "auth test"})
    assert (await run).result.status == "aborted"


async def mouse_click(page, selector: str) -> None:  # type: ignore[no-untyped-def]
    """A person's mouse on the headed window: raw input at the element's position, no automation checks."""
    box = await page.locator(selector).bounding_box()
    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await asyncio.sleep(0.2)


async def test_headed_window_accepts_input_only_while_a_human_holds_a_live_lease(
    balance_artifact: None, settings: Settings, target: str
) -> None:
    s, manager, server = await running_handoff(settings, 8798, Limits(claim_sla_s=60, human_active_max_s=60, heartbeat_s=0.5))  # type: ignore[arg-type]
    try:
        admin.arm(target, "unknown_modal")
        run = start_replay(s, manager)
        base = f"http://127.0.0.1:{s.operator_port}"
        async with operator_client(base) as c:
            iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
            page = manager.env.adapter.page  # type: ignore[union-attr]
            await mouse_click(page, "#att1")  # nobody has claimed the session: the fence swallows it
            assert not await page.is_checked("#att1")

            first = (await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "alice"})).json()
            await mouse_click(page, "#att1")  # alice holds a live lease: her input reaches the page
            assert await page.is_checked("#att1")

            await asyncio.sleep(2.0)  # no heartbeats: the lease expires after 3 x 0.5 s
            await mouse_click(page, "#ackbtn")  # a stale human cannot change the page
            assert await page.is_visible("#ackbtn")  # the modal is still there

            second = (await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "bob"})).json()
            assert second["token"] != first["token"]
            await c.post(
                f"/interventions/{iv['id']}/resolve", json={"operator_id": "bob", "kind": "abort", "token": second["token"], "note": "t"}
            )
        result = (await run).result
        assert result.status == "aborted"
        evs = events(s, result.run_id)
        assert any(e["type"] == "human_input_blocked" for e in evs)
        journaled = [e["data"]["action"] for e in evs if e["type"] == "human_action"]
        assert journaled and all((a["source"], a["operator"]) == ("headed_window", "alice") for a in journaled)
        assert not any(a.get("name") == "Acknowledge" for a in journaled)  # the stale click was blocked, so never journaled
    finally:
        await server.stop()


async def test_remote_keyboard_input_is_policy_checked_against_the_focused_control(
    balance_artifact: None, target: str, handoff: tuple[Settings, InterventionManager]
) -> None:
    s, manager = handoff
    admin.arm(target, "unknown_modal")
    run = start_replay(s, manager)
    base = f"http://127.0.0.1:{s.operator_port}"
    async with operator_client(base) as c:
        iv = await ScriptedOperator(base, api_token=TOKENS["supervisor-1"]).wait_for_intervention(c)
        token = (await c.post(f"/interventions/{iv['id']}/claim", json={"operator_id": "alice"})).json()["token"]
        try:
            page = manager.env.adapter.page  # type: ignore[union-attr]
            await page.focus("a[href='/logout']")  # Enter here would sign the session off
            press = {"operator_id": "alice", "token": token, "kind": "press", "key": "Enter"}
            denied = await c.post(f"/interventions/{iv['id']}/act", json=press)
            # This capability's effective allowlist (global ∩ tenant ∩ artifact) has no `press`: the human acting
            # inside the run is held to it, and the decision is made before the key is sent.
            assert denied.status_code == 403 and "blocked by policy" in denied.json()["detail"], denied.text
            assert "/logout" not in page.url
        finally:
            abort = {"operator_id": "alice", "kind": "abort", "token": token, "note": "t"}
            await c.post(f"/interventions/{iv['id']}/resolve", json=abort)
    result = (await run).result
    assert result.status == "aborted"
    blocked = [e for e in events(s, result.run_id) if e["type"] == "action_blocked"]
    assert blocked and "press on 'Sign Off'" in blocked[0]["summary"]  # evaluated against the focused control
