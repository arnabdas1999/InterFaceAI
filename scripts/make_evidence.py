"""Produce the /evidence package end to end.

  uv run python scripts/make_evidence.py            # genuine discovery with the planner configured in .env (Claude or Gemini)
  uv run python scripts/make_evidence.py --skip-discovery   # reuse committed capabilities; replays only (no key needed)

Every run is exported through the leak scanner. The discovery evidence is only ever produced by the
real model: the scripted planner is refused here on purpose.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request, Response  # noqa: E402  (module level: FastAPI resolves annotations from globals)

from interface_cua.config import Settings, load_settings  # noqa: E402
from interface_cua.discovery.compiler import AuthoredPatch, apply_patch, compile_trajectory, surface_of  # noqa: E402
from interface_cua.discovery.recorder import DiscoveryRequest  # noqa: E402
from interface_cua.discovery.review import render_review  # noqa: E402
from interface_cua.domain.canonical import canonical_json  # noqa: E402
from interface_cua.evidence.export import export_run, scan_dir  # noqa: E402
from interface_cua.handoff.manager import InterventionManager  # noqa: E402
from interface_cua.handoff.notifier import (  # noqa: E402
    ConsoleNotifier,
    FanoutNotifier,
    OutboxNotifier,
    WebhookNotifier,
    verify_signature,
)
from interface_cua.handoff.operator_api import OperatorServer  # noqa: E402
from interface_cua.handoff.scripted_operator import OperatorStep, ScriptedOperator  # noqa: E402
from interface_cua.profiles.store import ProfileStore  # noqa: E402
from interface_cua.replay.engine import InvocationRequest, ReplayEngine  # noqa: E402
from interface_cua.secrets.provider import EnvSecretProvider  # noqa: E402
from interface_cua.storage.capability_store import CapabilityStore  # noqa: E402
from target_app import admin  # noqa: E402

EVIDENCE = ROOT / "evidence"
INDEX: list[dict[str, Any]] = []
LEAK_LITERALS = ["Avery Quinn", "Jordan Blake", "Riley Moss", "synthetic-only-pass"]
SUB_INPUTS = {"member_id": "67890", "product": "share_certificate", "nickname": "CD Ladder"}


def note(folder: str, proves: str, run: Any, command: str, extra: dict[str, Any] | None = None) -> None:
    r = run.result if hasattr(run, "result") else run
    INDEX.append(
        {
            "folder": folder,
            "proves": proves,
            "run_id": r.run_id,
            "status": r.status,
            "code": getattr(r, "code", None),
            "command": command,
            **(extra or {}),
        }
    )


def export(settings: Settings, run_id: str, folder: str) -> None:
    export_run(settings, run_id, folder, forbidden=["Avery Quinn", "Jordan Blake", "Riley Moss", "synthetic-only-pass"])


def write(folder: str, name: str, data: Any) -> None:
    path = EVIDENCE / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def discover(settings: Settings, request_file: str, examples: dict[str, str], folder: str) -> Any:
    from interface_cua.discovery.runner import run_discovery
    from interface_cua.llm.factory import make_planner

    req = DiscoveryRequest.model_validate_json((ROOT / request_file).read_text(encoding="utf-8"))
    planner = make_planner()
    out = await run_discovery(settings, req, planner, examples)
    print(f"discovery {req.capability_id}: {out.result.status} {getattr(out.result, 'message', '')}")
    if out.result.status != "success":
        export(settings, out.result.run_id, f"_failed-{folder}")
        raise SystemExit(f"genuine discovery failed; evidence kept in evidence/_failed-{folder}")
    store = CapabilityStore(settings.capabilities_dir)
    tenant_profile = ProfileStore(settings).tenant(req.tenant_id)
    profile = ProfileStore(settings).app_profile(tenant_profile.app_profile)
    artifact = compile_trajectory(out.trajectory, profile, surface_type=surface_of(tenant_profile))
    store.save(artifact, render_review(artifact))
    export(settings, out.result.run_id, folder)
    write(
        folder,
        "caller-response.json",
        {"note": "What the caller received (synthetic data). Persisted copies are masked.", "outputs": out.outputs},
    )
    note(
        folder,
        f"Genuine LLM ({out.result.usage.model if out.result.usage else '?'}) discovery run compiled into {artifact.ref}",
        out,
        f"uv run cua discover --request {request_file} " + " ".join(f"--example {k}={v}" for k, v in examples.items()),
        {"usage": out.result.usage.model_dump() if out.result.usage else None},
    )
    return out


async def replay(
    settings: Settings, capability: str, inputs: dict[str, str], *, escalator: Any = None, tenant: str = "tenant-a",
    fallback: Any = None, **kw: Any,
) -> Any:  # fmt: skip
    return await ReplayEngine(settings, escalator=escalator, fallback=fallback).invoke(
        InvocationRequest(capability_id=capability, tenant_id=tenant, inputs=inputs, caller_id="evidence-script", **kw)
    )


async def validate(
    settings: Settings,
    cap: str,
    version: str,
    good: list[dict[str, str]],
    negative: list[tuple[dict[str, str], str]],
    expected: dict[str, Any] | None,
    folder: str,
    tenant: str = "tenant-a",
) -> None:
    store = CapabilityStore(settings.capabilities_dir)
    art = store.load(cap, version)
    irreversible = art.contract.side_effects == "irreversible"
    runs = []
    ok = True
    for n, inputs in enumerate(good):
        out = await replay(
            settings,
            cap,
            inputs,
            version=version,
            mode="validation",
            allow_unapproved=True,
            stop_before_irreversible=irreversible,
            tenant=tenant,
        )
        passed = out.result.status == "success" and (n > 0 or expected is None or out.outputs == expected)
        ok &= passed
        runs.append(
            {
                "kind": "good",
                "run_id": out.result.run_id,
                "status": out.result.status,
                "passed": passed,
                "outputs_match_discovery": (out.outputs == expected) if n == 0 and expected is not None else None,
                "validation_stop": getattr(out.result, "validation_stop", None),
            }
        )
        export(settings, out.result.run_id, f"{folder}/{out.result.run_id}")
    for inputs, want in negative:
        out = await replay(
            settings,
            cap,
            inputs,
            version=version,
            mode="validation",
            allow_unapproved=True,
            stop_before_irreversible=irreversible,
            tenant=tenant,
        )
        passed = out.result.status == "business_outcome" and getattr(out.result, "code", None) == want
        ok &= passed
        runs.append(
            {
                "kind": "negative",
                "expected": want,
                "run_id": out.result.run_id,
                "status": out.result.status,
                "code": getattr(out.result, "code", None),
                "passed": passed,
            }
        )
        export(settings, out.result.run_id, f"{folder}/{out.result.run_id}")
    if not ok:
        raise SystemExit(f"validation of {cap}@{version} failed: {runs}")
    if art.lifecycle.status == "draft":
        art = store.transition(
            art,
            "validated",
            actor="cua validate (evidence script)",
            note="automatic validation replays passed",
            run_ids=[r["run_id"] for r in runs],
        )
    if art.lifecycle.status == "validated":
        art = store.transition(art, "approved", actor="demo-reviewer", note="reviewed the review summary and validation runs")
    store.path(cap, version).with_suffix(".review.md").write_text(render_review(art), encoding="utf-8")
    write(
        folder,
        f"validation-report-{cap}-{version}.json",
        {
            "capability": art.ref,
            "content_hash": art.content_hash,
            "mode": "stop_before_irreversible" if irreversible else "full",
            "runs": runs,
            "passed": ok,
            "final_status": art.lifecycle.status,
        },
    )
    INDEX.append(
        {
            "folder": folder,
            "proves": f"{art.ref} validated automatically then approved (hash-bound)",
            "run_id": ",".join(r["run_id"] for r in runs),
            "status": art.lifecycle.status,
            "code": None,
            "command": f"uv run cua validate {cap} --version {version} ... && uv run cua approve {cap} --version {version} --approver demo-reviewer",
        }
    )


def note_existing(folder: str, request_file: str, examples: dict[str, str]) -> None:
    """Index entry for a genuine discovery run produced by an earlier invocation of this script."""
    result = json.loads((EVIDENCE / folder / "run-result.json").read_text(encoding="utf-8"))
    usage = result.get("usage") or {}
    INDEX.append({
        "folder": folder,
        "proves": f"Genuine LLM ({usage.get('model', '?')}) discovery run compiled into {result.get('capability')}@1.0.0",
        "run_id": result["run_id"], "status": result["status"], "code": result.get("code"),
        "command": f"uv run cua discover --request {request_file} " + " ".join(f"--example {k}={v}" for k, v in examples.items()),
        "usage": usage,
    })  # fmt: skip


def discovery_outputs(folder: str = "discovery-success") -> dict[str, Any] | None:
    """Verified discovery outputs; the first validation replay must reproduce them exactly."""
    path = EVIDENCE / folder / "caller-response.json"
    return json.loads(path.read_text(encoding="utf-8"))["outputs"] if path.exists() else None


WEBHOOK_KEY = "evidence-webhook-signing-key"  # local demo receiver only
RECEIVED_HOOKS: list[dict[str, Any]] = []


def start_webhook_receiver(port: int) -> Any:
    import threading

    import uvicorn

    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        body = await request.body()
        ok = verify_signature(WEBHOOK_KEY, request.headers.get("x-cua-timestamp", ""), body, request.headers.get("x-cua-signature", ""))
        payload = json.loads(body)
        RECEIVED_HOOKS.append(
            {
                "signature_valid": ok,
                "type": payload.get("type"),
                "intervention_id": payload.get("intervention_id"),
                "kind": payload.get("kind"),
                "tenant_id": payload.get("tenant_id"),
                "console": payload.get("console"),
            }
        )
        return Response(status_code=204 if ok else 401)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    return server


async def operator_auth_checks(manager: InterventionManager, settings: Settings, out: Any) -> dict[str, Any]:
    """Record how the operator API treats unauthenticated, out-of-scope, and under-privileged callers."""
    import httpx

    iv_id = out.result.interventions[0].intervention_id
    base = f"http://127.0.0.1:{settings.operator_port}"
    alice, bob = manager.directory.create_token("alice"), manager.directory.create_token("bob")
    cases = [
        ("no token", "GET", "/interventions", None, None),
        ("forged token", "GET", f"/interventions/{iv_id}", "cuaop_forged", None),
        ("operator scoped to another tenant (bob: tenant-b)", "GET", f"/interventions/{iv_id}", bob, None),
        ("plain operator tries to approve (alice: no supervisor role)", "POST", f"/interventions/{iv_id}/resolve", alice,
         {"kind": "approve", "action_hash": "x"}),
        ("request body claims another identity", "POST", f"/interventions/{iv_id}/claim", alice, {"operator_id": "supervisor-1"}),
    ]  # fmt: skip
    checks = []
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        for label, method, path, token, body in cases:
            headers = {"authorization": f"Bearer {token}"} if token else {}
            r = await c.request(method, path, headers=headers, json=body)
            checks.append({"case": label, "status": r.status_code, "detail": r.json().get("detail")})
    return {
        "note": "Identity comes from bearer tokens (only hashes are stored); roles and tenant scope are enforced on every call",
        "checks": checks,
    }


async def extras(settings: Settings, url: str) -> None:
    """Heterogeneity and reuse: tenant-b (vocabulary + reviewed override), tenant-c (frameset UI via the
    legacy-web adapter), assisted fallback, drift, catalog mining, and the desktop client."""
    from interface_cua.evidence.catalog_mining import list_candidates
    from interface_cua.evidence.drift import drift_report, load_results, render_markdown
    from interface_cua.llm.factory import PlannerConfigError, make_planner
    from target_app.server import start_in_thread

    started = []
    for port, tenant in ((8775, "tenant-b"), (8785, "tenant-c")):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                started.append(start_in_thread(port, tenant))
        admin.reset(f"http://127.0.0.1:{port}")

    def scenario(folder: str, proves: str, command: str, out: Any, extra_files: dict[str, Any] | None = None) -> None:
        shutil.rmtree(EVIDENCE / folder, ignore_errors=True)
        export(settings, out.result.run_id, folder)  # every config variant writes to the same runs dir
        if out.outputs:
            write(folder, "caller-response.json", {"note": "What the calling agent received (synthetic data).", "outputs": out.outputs})
        for name, data in (extra_files or {}).items():
            write(folder, name, data)
        note(folder, proves, out, command)
        print(folder, out.result.status, getattr(out.result, "code", ""))

    no_override = settings.runs_dir / "_config-no-override"
    shutil.rmtree(no_override, ignore_errors=True)
    shutil.copytree(settings.config_dir, no_override)
    tenant_b = json.loads((no_override / "tenants" / "tenant-b.json").read_text(encoding="utf-8"))
    tenant_b["override_refs"] = []
    (no_override / "tenants" / "tenant-b.json").write_text(json.dumps(tenant_b), encoding="utf-8")
    bare = dataclasses.replace(settings, config_dir=no_override)
    try:
        out = await replay(settings, "read-savings-balance", {"member_id": "67890"}, tenant="tenant-b")
        scenario(
            "replay-tenant-b",
            "Artifact recorded on tenant-a runs on tenant-b (same product, other vocabulary) with one reviewed, hash-approved "
            "override patch for a structurally different control",
            "uv run cua replay read-savings-balance --tenant tenant-b --input member_id=67890",
            out,
        )
        out = await replay(bare, "read-savings-balance", {"member_id": "67890"}, tenant="tenant-b")
        scenario(
            "replay-tenant-b-no-override",
            "Why the override exists: without it the recorded search control is not found on tenant-b, and the failure names "
            "the step and each locator candidate's match count",
            "(tenant-b with override_refs removed) uv run cua replay read-savings-balance --tenant tenant-b --input member_id=67890",
            out,
        )
        try:
            from interface_cua.assist.fallback import LLMFallbackAdvisor

            advisor: Any = LLMFallbackAdvisor(make_planner())
        except PlannerConfigError:
            advisor = None
        if advisor is not None:
            out = await replay(
                bare, "read-savings-balance", {"member_id": "67890"}, tenant="tenant-b", fallback=advisor, assisted_fallback=True
            )
            proposal = bare.runs_dir / out.result.run_id / "proposed-override-s2.json"
            scenario(
                "replay-assisted-fallback",
                "Opt-in bounded fallback: one policy-checked model suggestion found tenant-b's search control; the run succeeded "
                "and the new locator was saved as an UNAPPROVED override proposal (the artifact is unchanged)",
                "uv run cua replay read-savings-balance --tenant tenant-b --input member_id=67890 --assisted-fallback",
                out,
                {
                    "proposal-status.json": {
                        "proposal_saved": proposal.exists(),
                        "applied_to_artifact": False,
                        "needs": "review, then `cua overrides approve`, before any future run uses it",
                    }
                },
            )
        out = await replay(settings, "read-savings-balance", {"member_id": "67890"}, tenant="tenant-c")
        scenario(
            "replay-legacy-frameset",
            "The same artifact on tenant-c, which runs the product's classic frameset UI: the legacy-web adapter anchors frame "
            "paths, URL checks and navigation to the application frame",
            "uv run cua replay read-savings-balance --tenant tenant-c --input member_id=67890",
            out,
        )
        admin.reset(url)
        admin.arm(url, "caption_drift")
        out = await replay(settings, "read-savings-balance", {"member_id": "12345"})
        scenario(
            "replay-drift",
            "A re-captioned control: the run still succeeds through a lower-ranked locator and reports locator_fallback_used "
            "(drift becomes visible before it becomes failure)",
            "uv run cua target faults set caption_drift && uv run cua replay read-savings-balance --input member_id=12345",
            out,
        )
        admin.reset(url)
        admin.arm(url, "unknown_modal")
        out = await replay(settings, "read-savings-balance", {"member_id": "12345"})
        scenario(
            "replay-unknown-state",
            "Unknown modal with escalation_mode=fail_fast: failure/unknown_state with evidence; the state is mined into a "
            "reviewable catalog candidate (see catalog-candidates/)",
            "uv run cua target faults set unknown_modal && uv run cua replay read-savings-balance --input member_id=12345",
            out,
        )
        if sys.platform == "win32" and CapabilityStore(settings.capabilities_dir).versions("desktop-read-savings-balance"):
            out = await replay(settings, "desktop-read-savings-balance", {"member_id": "67890"}, tenant="tenant-d")
            scenario(
                "replay-desktop",
                "Deterministic replay of the desktop capability through UI Automation (masked window screenshots; sanitized "
                "UIA tree as the failure signal)",
                "uv run cua replay desktop-read-savings-balance --tenant tenant-d --input member_id=67890",
                out,
            )
    finally:
        for server in started:
            server.stop()

    cand_dir = EVIDENCE / "catalog-candidates"
    shutil.rmtree(cand_dir, ignore_errors=True)
    cand_dir.mkdir()
    for cand in list_candidates(settings.state_dir / "catalog-candidates"):
        (cand_dir / f"{cand['candidate_id']}.json").write_text(json.dumps(cand, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report = drift_report(load_results(sorted(p for p in EVIDENCE.rglob("*") if (p / "run-result.json").exists())))
    (EVIDENCE / "drift").mkdir(exist_ok=True)
    (EVIDENCE / "drift" / "drift-report.md").write_text(render_markdown(report), encoding="utf-8")
    write("drift", "drift-report.json", report)
    INDEX.append(
        {
            "folder": "drift",
            "proves": "Replay health per capability x tenant over every evidence run; alerts on fallback-locator use",
            "run_id": "-",
            "status": f"{report['runs']} runs",
            "code": None,
            "command": "uv run cua drift report --runs-dir evidence --out evidence/drift/drift-report.md",
        }
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-discovery", action="store_true")
    ap.add_argument("--rediscover", action="store_true", help="re-run discovery even if a capability already exists")
    ap.add_argument("--stability-runs", type=int, default=10)
    args = ap.parse_args()
    settings = load_settings()
    running = None
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8765)) != 0:
            from target_app.server import start_in_thread

            running = start_in_thread(8765)
    url = "http://127.0.0.1:8765"
    admin.reset(url)
    EVIDENCE.mkdir(exist_ok=True)
    store = CapabilityStore(settings.capabilities_dir)
    expected = None

    # 1. genuine discovery (the model) -> draft artifacts
    if not args.skip_discovery:
        from interface_cua.llm.factory import PlannerConfigError, make_planner

        try:
            make_planner()
        except PlannerConfigError as exc:
            raise SystemExit(f"{exc} (the genuine discovery run needs a model key; or pass --skip-discovery)") from None
        plan = [
            ("read-savings-balance", "requests/read-savings-balance.json", {"member_id": "12345"}, "discovery-success"),
            (
                "open-sub-account",
                "requests/open-sub-account.json",
                {"member_id": "12345", "product": "money_market", "nickname": "Vacation Fund"},
                "discovery-open-sub-account",
            ),
        ]
        if sys.platform == "win32":
            plan.append(
                ("desktop-read-savings-balance", "requests/desktop-read-savings-balance.json", {"member_id": "12345"}, "discovery-desktop")
            )
        for cap, request_file, examples, folder in plan:
            # Free tiers have small daily quotas: a capability that already has a genuine discovery is
            # kept unless --rediscover is passed.
            if store.versions(cap) and not args.rediscover:
                print(f"discovery {cap}: kept existing genuine run in evidence/{folder}")
                note_existing(folder, request_file, examples)
                continue
            shutil.rmtree(EVIDENCE / folder, ignore_errors=True)
            shutil.rmtree(settings.capabilities_dir / cap, ignore_errors=True)
            await discover(settings, request_file, examples, folder)
            admin.reset(url)
    expected = discovery_outputs()

    # 2. authored irreversible commit step -> open-sub-account@2.0.0
    if not store.path("open-sub-account", "2.0.0").exists():
        base = store.load("open-sub-account", "1.0.0")
        patch = AuthoredPatch.model_validate_json(
            (ROOT / "apps/synthcore/authored/open-sub-account.commit.json").read_text(encoding="utf-8")
        )
        v2 = apply_patch(base, patch, ProfileStore(settings).app_profile("synthcore@1.0.0"))
        store.save(v2, render_review(v2))

    # 3. validation + approval
    shutil.rmtree(EVIDENCE / "validation", ignore_errors=True)
    await validate(
        settings,
        "read-savings-balance",
        "1.0.0",
        [{"member_id": "12345"}, {"member_id": "67890"}],
        [({"member_id": "99999"}, "member_not_found")],
        expected,
        "validation",
    )
    await validate(
        settings,
        "open-sub-account",
        "2.0.0",
        [SUB_INPUTS, {"member_id": "24680", "product": "money_market", "nickname": "Rainy Day"}],
        [({"member_id": "99999", "product": "money_market", "nickname": "X"}, "member_not_found")],
        None,
        "validation",
    )
    assert admin.ledger(url) == [], "validation must never commit"
    if sys.platform == "win32" and store.versions("desktop-read-savings-balance"):
        await validate(
            settings, "desktop-read-savings-balance", "1.0.0", [{"member_id": "12345"}, {"member_id": "67890"}],
            [({"member_id": "99999"}, "member_not_found")], discovery_outputs("discovery-desktop"), "validation", tenant="tenant-d",
        )  # fmt: skip

    # 4. replays
    scenarios: list[tuple[str, str, dict[str, str], dict[str, dict[str, Any]], str]] = [
        (
            "replay-success",
            "Deterministic replay with a new input returns a typed money output; no model events",
            {"member_id": "67890"},
            {},
            "uv run cua replay read-savings-balance --input member_id=67890",
        ),
        (
            "replay-business-outcome",
            "'No such member' is a business outcome, not a crash",
            {"member_id": "99999"},
            {},
            "uv run cua replay read-savings-balance --input member_id=99999",
        ),
        (
            "replay-recovered",
            "Session expiry (re-auth + re-entry), slow load, transient HTTP 500 and a known interstitial are recovered within bounds",
            {"member_id": "24680"},
            {"session_expire": {"skip": 2}, "slow_search": {}, "http_500": {"route": "lookup"}, "system_notice": {}},
            "uv run cua target faults set session_expire --param skip=2 (+ slow_search, http_500 --param route=lookup, system_notice) && uv run cua replay ...",
        ),
        (
            "replay-hard-failure",
            "Permission denial is a non-retryable hard failure with step/expected/observed and masked evidence",
            {"member_id": "12345"},
            {"permission_denied": {}},
            "uv run cua target faults set permission_denied && uv run cua replay read-savings-balance --input member_id=12345",
        ),
        (
            "replay-rejected",
            "Malformed input is rejected pre-flight; no browser session is opened",
            {"member_id": "12ab"},
            {},
            "uv run cua replay read-savings-balance --input member_id=12ab",
        ),
    ]
    for folder, proves, inputs, faults, command in scenarios:
        admin.reset(url)
        for name, params in faults.items():
            admin.arm(url, name, **params)
        out = await replay(settings, "read-savings-balance", inputs)
        shutil.rmtree(EVIDENCE / folder, ignore_errors=True)
        run_dir = settings.runs_dir / out.result.run_id
        if (run_dir / "events.jsonl").exists():
            export(settings, out.result.run_id, folder)
        else:
            (EVIDENCE / folder).mkdir(parents=True, exist_ok=True)
            shutil.copy(run_dir / "run-result.json", EVIDENCE / folder / "run-result.json")
        if out.outputs:
            write(
                folder,
                "caller-response.json",
                {
                    "note": "What the calling agent received (synthetic data). The persisted run-result.json is masked.",
                    "outputs": out.outputs,
                },
            )
        note(folder, proves, out, command)
        print(folder, out.result.status, getattr(out.result, "code", ""))

    # 5. same-session human handoff (scripted stand-in for a person, via the operator API).
    # Routing: console + outbox + a signed webhook to a local receiver that verifies every delivery.
    start_webhook_receiver(8797)
    webhook = WebhookNotifier("http://127.0.0.1:8797/hook", WEBHOOK_KEY, backoff_s=0.2)
    manager = InterventionManager(
        settings, notifier=FanoutNotifier(ConsoleNotifier(), OutboxNotifier(settings.state_dir / "outbox.jsonl"), webhook)
    )
    server = OperatorServer(manager, settings.operator_port)
    await server.start()
    try:
        admin.reset(url)
        admin.arm(url, "unknown_modal")
        op = ScriptedOperator(
            f"http://127.0.0.1:{settings.operator_port}", "scripted-operator", manager.directory.create_token("scripted-operator")
        )
        task = asyncio.create_task(
            op.run(
                [
                    OperatorStep("click_control", name="I have a permissible purpose", role="checkbox"),
                    OperatorStep("click_control", name="Acknowledge", role="button"),
                    OperatorStep("resolve", resolution="resume", note="completed the compliance attestation; handing back"),
                ]
            )
        )
        out = await replay(settings, "read-savings-balance", {"member_id": "12345"}, escalator=manager, escalation_mode="escalate")
        op_log = await task
        shutil.rmtree(EVIDENCE / "replay-handoff", ignore_errors=True)
        export(settings, out.result.run_id, "replay-handoff")
        write(
            "replay-handoff",
            "operator-transcript.json",
            {
                "operator_kind": "scripted_stand_in",
                "note": "API calls made by the scripted operator; a person uses the console or the headed window",
                **op_log,
            },
        )
        write(
            "replay-handoff",
            "webhook-deliveries.json",
            {
                "note": "Intervention routed to a webhook; the receiver verified the HMAC signature and timestamp of each delivery",
                "sender": webhook.deliveries,
                "receiver": RECEIVED_HOOKS,
            },
        )
        write("replay-handoff", "operator-auth-checks.json", await operator_auth_checks(manager, settings, out))
        note(
            "replay-handoff",
            "Unknown modal -> intervention -> operator claims the SAME live session (fenced lease) -> actions journaled -> handback -> resume point -> success",
            out,
            "terminal 1: uv run cua replay read-savings-balance --input member_id=12345 --escalate ; terminal 2: uv run cua operator ...",
        )
        print("replay-handoff", out.result.status)

        # 6. approval-bound irreversible step
        admin.reset(url)
        op = ScriptedOperator(f"http://127.0.0.1:{settings.operator_port}", "supervisor-1", manager.directory.create_token("supervisor-1"))
        task = asyncio.create_task(op.run([OperatorStep("resolve", resolution="approve", note="confirmed with the member")]))
        out = await replay(settings, "open-sub-account", SUB_INPUTS, escalator=manager, escalation_mode="escalate")
        op_log = await task
        shutil.rmtree(EVIDENCE / "replay-approval", ignore_errors=True)
        export(settings, out.result.run_id, "replay-approval")
        write(
            "replay-approval",
            "ledger-after.json",
            {
                "note": "Target app commit ledger after the run: exactly one commit (account number masked to last 4)",
                "entries": [{**e, "account_number": "******" + str(e.get("account_number", ""))[-4:]} for e in admin.ledger(url)],
            },
        )
        write("replay-approval", "operator-transcript.json", {"operator_kind": "scripted_stand_in", **op_log})
        if out.outputs:
            write("replay-approval", "caller-response.json", {"outputs": out.outputs})
        note(
            "replay-approval",
            "Irreversible commit paused for a single-use, hash-bound approval; exactly one commit after approval",
            out,
            "uv run cua replay open-sub-account --input member_id=67890 --input product=share_certificate --input nickname='CD Ladder' --escalate",
        )
        print("replay-approval", out.result.status, len(admin.ledger(url)))
    finally:
        await server.stop()

    # 7. stability
    admin.reset(url)
    outs = [await replay(settings, "read-savings-balance", {"member_id": "12345"}) for _ in range(args.stability_runs)]
    durations = [o.result.duration_ms for o in outs]
    write(
        "stability",
        "stability-report.json",
        {
            "capability": outs[0].result.capability,
            "runs": len(outs),
            "passed": sum(o.result.status == "success" for o in outs),
            "outputs_identical": len({json.dumps(o.outputs, sort_keys=True) for o in outs}) == 1,
            "drift_signals": sum(len(getattr(o.result, "drift", [])) for o in outs),
            "duration_ms": {"min": min(durations), "max": max(durations), "mean": int(sum(durations) / len(durations))},
            "run_ids": [o.result.run_id for o in outs],
        },
    )
    INDEX.append(
        {
            "folder": "stability",
            "proves": f"{len(outs)} consecutive replays, pass count and timing spread",
            "run_id": "-",
            "status": f"{sum(o.result.status == 'success' for o in outs)}/{len(outs)} success",
            "code": None,
            "command": "uv run cua stability read-savings-balance --input member_id=12345 --runs 10",
        }
    )

    # 8. heterogeneity, reuse, drift (see REPORT: Heterogeneity & multi-tenant)
    await extras(settings, url)
    for op_id in ("scripted-operator", "supervisor-1", "alice", "bob"):
        manager.directory.revoke(op_id)  # tokens issued for this script are not left behind

    # 9. artifacts + index
    art_dir = EVIDENCE / "artifacts"
    shutil.rmtree(art_dir, ignore_errors=True)
    art_dir.mkdir()
    for a in store.list():
        (art_dir / f"{a.capability.id}.v{a.capability.version}.json").write_text(
            canonical_json(a.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
        )
        (art_dir / f"{a.capability.id}.v{a.capability.version}.review.md").write_text(render_review(a), encoding="utf-8")
    existing = json.loads((EVIDENCE / "index.json").read_text(encoding="utf-8")) if (EVIDENCE / "index.json").exists() else []
    # Folders not regenerated this time (kept discoveries; the assisted-fallback run without a key) keep their entry.
    kept = [e for e in existing if not any(i["folder"] == e["folder"] for i in INDEX) and (EVIDENCE / e["folder"]).exists()]
    (EVIDENCE / "index.json").write_text(json.dumps(kept + INDEX, indent=2) + "\n", encoding="utf-8")
    if running:
        running.stop()
    final = scan_dir(
        EVIDENCE,
        secrets=EnvSecretProvider().all_values("synthcore/operator"),
        forbidden=LEAK_LITERALS,
        profile=ProfileStore(settings).app_profile("synthcore"),
    )
    if final:
        raise SystemExit(f"leak scan over evidence/ failed: {final}")
    print("evidence written; see evidence/index.json")


if __name__ == "__main__":
    asyncio.run(main())
