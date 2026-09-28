"""One artifact recorded on the base tenant, replayed on a second tenant running the same product with
different vocabulary and a structurally different control (fixed by a reviewed override patch)."""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from interface_cua.assist.fallback import LLMFallbackAdvisor
from interface_cua.config import Settings
from interface_cua.discovery.compiler import compile_trajectory
from interface_cua.discovery.runner import run_discovery
from interface_cua.domain.overrides import OverridePatch
from interface_cua.llm.client import DiscoveryContext, DiscoveryDecision
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from target_app import admin
from tests.helpers import balance_request, balance_script, handle_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def target_b() -> Iterator[str]:
    running = None
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8775)) != 0:
            from target_app.server import start_in_thread

            running = start_in_thread(8775, "tenant-b")
    admin.reset("http://127.0.0.1:8775")
    yield "http://127.0.0.1:8775"
    if running:
        running.stop()


@pytest.fixture(scope="module")
async def base_artifact(target: str, settings: Settings) -> None:
    store = CapabilityStore(settings.capabilities_dir)
    if store.path("read-savings-balance", "1.0.0").exists():
        return
    out = await run_discovery(settings, balance_request(), FakePlanner(balance_script), {"member_id": "12345"})
    art = compile_trajectory(out.trajectory, ProfileStore(settings).app_profile("synthcore@1.0.0"))  # type: ignore[arg-type]
    store.save(art)
    store.transition(store.transition(art, "validated", actor="t"), "approved", actor="t")


def config_copy(settings: Settings, tmp_path: Path, mutate) -> Settings:  # type: ignore[no-untyped-def]
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    mutate(cfg)
    return dataclasses.replace(settings, config_dir=cfg)


async def replay_b(settings: Settings, member_id: str = "67890"):  # type: ignore[no-untyped-def]
    return await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="read-savings-balance", tenant_id="tenant-b", inputs={"member_id": member_id})
    )


async def test_base_artifact_runs_on_tenant_b_with_vocabulary_and_override(base_artifact: None, target_b: str, settings: Settings) -> None:
    out = await replay_b(settings)
    r = out.result
    assert r.status == "success", r
    assert out.outputs == {"savings_balance": {"amount": "15020.00", "currency": "USD"}}
    assert r.overrides and r.overrides[0].startswith("tenant-b/read-savings-balance/search-button@1.0.0#")
    base = CapabilityStore(settings.capabilities_dir).load("read-savings-balance", "1.0.0")
    assert r.content_hash == base.content_hash  # identity stays the approved base artifact


async def test_without_the_override_tenant_b_fails_with_locator_diagnostics(
    base_artifact: None, target_b: str, settings: Settings, tmp_path: Path
) -> None:
    def drop(cfg: Path) -> None:
        p = cfg / "tenants" / "tenant-b.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["override_refs"] = []
        p.write_text(json.dumps(data), encoding="utf-8")

    out = await replay_b(config_copy(settings, tmp_path, drop))
    r = out.result
    assert r.status == "failure" and r.code == "locator_not_found" and r.step_id == "s2", r  # type: ignore[union-attr]
    assert {d.strategy: d.match_count for d in r.locator_diagnostics}["role_name"] == 0  # type: ignore[union-attr]


async def test_an_override_edited_after_approval_is_refused(base_artifact: None, target_b: str, settings: Settings, tmp_path: Path) -> None:
    def tamper(cfg: Path) -> None:
        p = cfg / "overrides" / "tenant-b" / "read-savings-balance.search-button.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["ops"][0]["candidates"][0]["name"] = "Delete Customer"  # an unreviewed edit
        p.write_text(json.dumps(data), encoding="utf-8")

    out = await replay_b(config_copy(settings, tmp_path, tamper))
    r = out.result
    assert r.status == "rejected" and r.code == "artifact_invalid" and "not approved" in r.errors[0].message  # type: ignore[union-attr]


def _without_override(cfg: Path) -> None:
    p = cfg / "tenants" / "tenant-b.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["override_refs"] = []
    p.write_text(json.dumps(data), encoding="utf-8")


def _pick(name: str, role: str) -> FakePlanner:
    def planner(ctx: DiscoveryContext) -> DiscoveryDecision:
        assert "RECOVER ONE RECORDED STEP" in ctx.goal and "Click button 'Search'" in ctx.goal
        return DiscoveryDecision(action="click", handle=handle_for(ctx, name, role), rationale=f"'{name}' submits the search")

    return FakePlanner(planner)


async def test_assisted_fallback_recovers_once_and_proposes_an_unapproved_override(
    base_artifact: None, target_b: str, settings: Settings, tmp_path: Path
) -> None:
    s = config_copy(settings, tmp_path, _without_override)
    fake = _pick("Find", "button")
    out = await ReplayEngine(s, fallback=LLMFallbackAdvisor(fake)).invoke(
        InvocationRequest(capability_id="read-savings-balance", tenant_id="tenant-b", inputs={"member_id": "67890"}, assisted_fallback=True)
    )
    r = out.result
    assert r.status == "success", r
    assert len(fake.calls) == 1  # bounded: a single model call
    assert [x.code for x in r.recoveries if x.step_id == "s2"] == ["assisted_fallback"]  # type: ignore[union-attr]
    assert any(d.code == "assisted_fallback" for d in r.drift)  # type: ignore[union-attr]
    proposal = OverridePatch.model_validate_json((s.runs_dir / r.run_id / "proposed-override-s2.json").read_text(encoding="utf-8"))
    assert not proposal.is_approved() and proposal.ops[0].step == "s2"
    assert CapabilityStore(s.capabilities_dir).load("read-savings-balance", "1.0.0").hash_ok()  # the artifact is untouched


async def test_assisted_fallback_rejects_a_suggestion_with_the_wrong_role(
    base_artifact: None, target_b: str, settings: Settings, tmp_path: Path
) -> None:
    s = config_copy(settings, tmp_path, _without_override)
    out = await ReplayEngine(s, fallback=LLMFallbackAdvisor(_pick("Sign Off", "link"))).invoke(
        InvocationRequest(capability_id="read-savings-balance", tenant_id="tenant-b", inputs={"member_id": "67890"}, assisted_fallback=True)
    )
    assert out.result.status == "failure" and out.result.code == "locator_not_found"  # type: ignore[union-attr]


async def test_assisted_fallback_is_never_consulted_unless_opted_in(
    base_artifact: None, target_b: str, settings: Settings, tmp_path: Path
) -> None:
    s = config_copy(settings, tmp_path, _without_override)
    fake = _pick("Find", "button")
    out = await ReplayEngine(s, fallback=LLMFallbackAdvisor(fake)).invoke(
        InvocationRequest(capability_id="read-savings-balance", tenant_id="tenant-b", inputs={"member_id": "67890"})
    )
    assert out.result.status == "failure" and not fake.calls


async def test_non_default_credentials_drive_both_the_target_and_the_login_routine(
    base_artifact: None, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One .env setting (CUA_SECRET_SYNTHCORE_OPERATOR_*) configures the target and the automation."""
    import httpx

    from target_app.server import start_in_thread

    monkeypatch.setenv("CUA_SECRET_SYNTHCORE_OPERATOR_USERNAME", "teller7")
    monkeypatch.setenv("CUA_SECRET_SYNTHCORE_OPERATOR_PASSWORD", "another-synthetic-pass")
    running = start_in_thread(8769)  # the target reads the variables when it is created

    def point_tenant_a_at_8769(cfg: Path) -> None:
        tenant = json.loads((cfg / "tenants" / "tenant-a.json").read_text(encoding="utf-8"))
        tenant["base_url"] = "http://127.0.0.1:8769"
        (cfg / "tenants" / "tenant-a.json").write_text(json.dumps(tenant), encoding="utf-8")

    try:
        s = config_copy(settings, tmp_path, point_tenant_a_at_8769)
        out = await ReplayEngine(s).invoke(
            InvocationRequest(capability_id="read-savings-balance", tenant_id="tenant-a", inputs={"member_id": "67890"})
        )
        assert out.result.status == "success", out.result
        old = httpx.post("http://127.0.0.1:8769/login", data={"usr": "operator1", "pwd": "synthetic-only-pass"})
        assert old.status_code == 401  # the defaults no longer work: both sides really use the new values
    finally:
        running.stop()
