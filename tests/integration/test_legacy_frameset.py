"""The same artifact on a tenant running the product's classic frameset UI (legacy-web adapter)."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from interface_cua.config import Settings
from interface_cua.discovery.compiler import compile_trajectory
from interface_cua.discovery.runner import run_discovery
from interface_cua.llm.fake_client import FakePlanner
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.engine import InvocationRequest, ReplayEngine
from interface_cua.storage.capability_store import CapabilityStore
from target_app import admin
from tests.helpers import balance_request, balance_script

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def target_c() -> Iterator[str]:
    running = None
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8785)) != 0:
            from target_app.server import start_in_thread

            running = start_in_thread(8785, "tenant-c")
    admin.reset("http://127.0.0.1:8785")
    yield "http://127.0.0.1:8785"
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


async def replay_c(settings: Settings, member_id: str):  # type: ignore[no-untyped-def]
    return await ReplayEngine(settings).invoke(
        InvocationRequest(capability_id="read-savings-balance", version="1.0.0", tenant_id="tenant-c", inputs={"member_id": member_id})
    )


async def test_artifact_recorded_on_modern_ui_replays_inside_a_frameset(base_artifact: None, target_c: str, settings: Settings) -> None:
    out = await replay_c(settings, "67890")
    r = out.result
    assert r.status == "success", r
    assert out.outputs == {"savings_balance": {"amount": "15020.00", "currency": "USD"}}
    assert not r.drift  # type: ignore[union-attr]  # same locators: only the frame anchoring differs


async def test_outcomes_and_failures_work_inside_the_frameset(base_artifact: None, target_c: str, settings: Settings) -> None:
    out = await replay_c(settings, "99999")
    assert out.result.status == "business_outcome" and out.result.code == "member_not_found"  # type: ignore[union-attr]
    admin.arm(target_c, "permission_denied")
    out = await replay_c(settings, "12345")
    assert out.result.status == "failure" and out.result.code == "permission_denied"  # type: ignore[union-attr]
