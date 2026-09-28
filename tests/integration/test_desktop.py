"""The same pipeline on a Windows desktop client through UI Automation (Windows only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from interface_cua.config import Settings
from interface_cua.discovery.compiler import compile_trajectory
from interface_cua.discovery.recorder import DiscoveryRequest
from interface_cua.discovery.runner import run_discovery
from interface_cua.domain.types import OutputType
from interface_cua.llm.client import CheckProposal, DiscoveryContext, DiscoveryDecision
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from tests.helpers import handle_for

pytestmark = [pytest.mark.integration, pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")]
REQUEST = "requests/desktop-read-savings-balance.json"


def desktop_script(ctx: DiscoveryContext) -> DiscoveryDecision:
    text, hist = ctx.observation_text, " ".join(ctx.history)
    if "Type input member_id" not in hist:
        return DiscoveryDecision(action="type", handle=handle_for(ctx, "Member ID", "textbox"), input_name="member_id")
    if "Click button 'Search'" not in hist:
        return DiscoveryDecision(action="click", handle=handle_for(ctx, "Search", "button"))
    if "savings_balance" not in ctx.extracted:
        assert "Share Savings Balance" in text
        return DiscoveryDecision(
            action="extract", output_name="savings_balance", output_type=OutputType.MONEY, label_text="Share Savings Balance"
        )
    return DiscoveryDecision(action="done", checkpoint=[CheckProposal(label_text="Member", equals_input="member_id")])


@pytest.fixture(scope="module")
async def desktop_artifact(settings: Settings) -> None:
    store = CapabilityStore(settings.capabilities_dir)
    if store.path("desktop-read-savings-balance", "1.0.0").exists():
        return
    req = DiscoveryRequest.model_validate_json(Path(REQUEST).read_text(encoding="utf-8"))
    planner = FakePlanner(desktop_script)
    out = await run_discovery(settings, req, planner, {"member_id": "12345"})
    assert out.result.status == "success", out.result
    assert "12345" not in "\n".join(c.observation_text for c in planner.calls)  # egress masking on the desktop surface too
    art = compile_trajectory(out.trajectory, ProfileStore(settings).app_profile("synthcore-desktop@1.0.0"), surface_type="desktop")  # type: ignore[arg-type]
    assert art.target.surface_type == "desktop" and "desktop-uia" in art.target.adapter_requirements
    typed = art.steps[0].target
    assert typed is not None and {c.strategy for c in typed.candidates} >= {"label_anchor", "attribute"}  # unnamed edit: label by geometry
    assert all("desktop-uia" in c.adapter_kinds for s in art.steps if s.target for c in s.target.candidates)
    assert {m.outcome_code for m in art.steps[1].states} == {"member_not_found", "validation_rejected"}
    store.save(art)
    store.transition(store.transition(art, "validated", actor="t"), "approved", actor="t")


async def replay(settings: Settings, member_id: str):  # type: ignore[no-untyped-def]
    return await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="desktop-read-savings-balance", tenant_id="tenant-d", inputs={"member_id": member_id})
    )


async def test_desktop_capability_replays_with_new_input(desktop_artifact: None, settings: Settings) -> None:
    out = await replay(settings, "67890")
    assert out.result.status == "success", out.result
    assert out.outputs == {"savings_balance": {"amount": "15020.00", "currency": "USD"}}
    shots = list((settings.runs_dir / out.result.run_id / "screenshots").glob("*.png"))
    assert shots  # masked window screenshot


async def test_desktop_business_outcome_and_hard_failure(desktop_artifact: None, settings: Settings) -> None:
    out = await replay(settings, "99999")
    assert out.result.status == "business_outcome" and out.result.code == "member_not_found"  # type: ignore[union-attr]
    os.environ["CUA_DESKTOP_FAULT"] = "permission_denied"
    try:
        out = await replay(settings, "12345")
    finally:
        del os.environ["CUA_DESKTOP_FAULT"]
    r = out.result
    assert r.status == "failure" and r.code == "permission_denied" and "ERR-SEC-403" in (r.observed or "")  # type: ignore[union-attr]
    assert any(e.path.endswith(".uia.json") for e in r.evidence)  # sanitized UIA tree as the rich failure signal


def test_window_capture_is_the_application_even_when_covered() -> None:
    """A screen grab of a covered window records whatever app is on top (another user's data) into evidence
    and model input. The capture must render the application window itself."""
    import ctypes
    import subprocess
    import time

    import uiautomation as auto
    from PIL import ImageStat

    from interface_cua.surfaces.desktop.adapter import _print_window

    launch = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "src/target_app/desktop/SynthCoreDesktop.ps1"]
    proc = subprocess.Popen(launch)
    try:
        with auto.UIAutomationInitializerInThread():
            win = None
            for _ in range(80):  # match our own process, never another window
                win = next((w for w in auto.GetRootControl().GetChildren() if w.ProcessId == proc.pid), None)
                if win is not None:
                    break
                time.sleep(0.25)
            assert win is not None and "SynthCore Desktop" in win.Name
            hwnd = win.NativeWindowHandle
            ctypes.windll.user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, no move/size/activate
            time.sleep(0.5)
            img, _origin = _print_window(hwnd)
            rect = win.BoundingRectangle
            assert img.size == (rect.width(), rect.height())
            # The form body is the light WinForms background, whatever is on screen above it.
            w, h = img.size
            body = img.convert("L").crop((int(w * 0.55), int(h * 0.7), int(w * 0.9), int(h * 0.9)))  # empty lower-right of the form
            assert ImageStat.Stat(body).stddev[0] < 12 and 225 <= ImageStat.Stat(body).mean[0] <= 250
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
