"""Drift detection (fallback-locator use surfaces before failure) and catalog mining of unknown states."""

from __future__ import annotations

import pytest

from interface_cua.config import Settings
from interface_cua.discovery.compiler import compile_trajectory
from interface_cua.discovery.runner import run_discovery
from interface_cua.evidence.catalog_mining import list_candidates
from interface_cua.evidence.drift import drift_report, load_results
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from target_app import admin
from tests.helpers import balance_request, balance_script

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


async def replay(settings: Settings, member_id: str = "12345"):  # type: ignore[no-untyped-def]
    return await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="read-savings-balance", version="1.0.0", tenant_id="tenant-a", inputs={"member_id": member_id})
    )


async def test_recaptioned_control_succeeds_via_fallback_and_is_reported_as_drift(
    balance_artifact: None, target: str, settings: Settings
) -> None:
    admin.arm(target, "caption_drift")
    out = await replay(settings)
    r = out.result
    assert r.status == "success", r
    assert [(d.code, d.step_id) for d in r.drift] == [("locator_fallback_used", "s3")]  # type: ignore[union-attr]
    report = drift_report(load_results([settings.runs_dir / r.run_id]))
    group = report["groups"][0]
    assert group["drift_signals"] == {"s3:locator_fallback_used": 1}
    assert any("locator drift" in a for a in group["alerts"])


async def test_unknown_states_become_deduplicated_catalog_candidates(balance_artifact: None, target: str, settings: Settings) -> None:
    for _ in range(2):
        admin.arm(target, "unknown_modal")
        out = await replay(settings)
        assert out.result.status == "failure" and out.result.code == "unknown_state"  # type: ignore[union-attr]
    candidates = [c for c in list_candidates(settings.state_dir / "catalog-candidates") if "attestation" in c["signature"]["title"].lower()]
    assert len(candidates) == 1
    cand = candidates[0]
    assert len(cand["occurrences"]) >= 2 and cand["status"] == "needs_review"
    assert cand["signature"]["title"] == "Compliance attestation required"  # the overlay heading, not its body
    assert cand["route"] == "/members/:member_id"  # generalized, no concrete member id
    assert "Acknowledge" in cand["signature"]["buttons"] and cand["signature"]["checkboxes"]
    suggested = cand["suggested_state"]
    assert suggested["handler"] is None and "human decision" in suggested["note"]  # an attestation is never auto-dismissed
