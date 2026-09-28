"""``cua`` command line. Exit codes: 0 success, 10 business_outcome, 20 rejected, 30 failure, 40 aborted, 2 usage."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import typer

from interface_cua.config import ROOT, Settings, load_settings
from interface_cua.domain.canonical import canonical_json
from interface_cua.domain.results import EXIT_CODES

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Computer-use automation: discover -> compile -> validate -> approve -> replay."
)
target_app = typer.Typer(no_args_is_help=True, help="Synthetic SynthCore target (local, synthetic data only).")
faults_app = typer.Typer(no_args_is_help=True, help="Out-of-band fault injection (never reachable by automation).")
caps_app = typer.Typer(no_args_is_help=True, help="Capability registry.")
op_app = typer.Typer(no_args_is_help=True, help="Operator side of a live handoff (talks to the run's operator API).")
evidence_app = typer.Typer(no_args_is_help=True, help="Evidence packaging.")
app.add_typer(target_app, name="target")
target_app.add_typer(faults_app, name="faults")
app.add_typer(caps_app, name="capabilities")
app.add_typer(op_app, name="operator")
app.add_typer(evidence_app, name="evidence")
llm_app = typer.Typer(no_args_is_help=True, help="Discovery planner configuration.")
app.add_typer(llm_app, name="llm")


drift_app = typer.Typer(no_args_is_help=True, help="Replay health and drift signals per capability x tenant.")
app.add_typer(drift_app, name="drift")
catalog_app = typer.Typer(no_args_is_help=True, help="Catalog candidates mined from unknown runtime states.")
app.add_typer(catalog_app, name="catalog")


@drift_app.command("report")
def drift_cmd(runs_dir: Path | None = None, out: Path | None = None) -> None:
    """Aggregate replay/validation results; alert on fallback-locator use, failure rates, incompatibility."""
    from interface_cua.evidence.drift import drift_report, load_results, render_markdown

    base = runs_dir or load_settings().runs_dir
    report = drift_report(load_results(sorted(p for p in base.rglob("*") if (p / "run-result.json").exists())))
    if out:
        out.write_text(render_markdown(report), encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    _emit(report)


@catalog_app.command("candidates")
def catalog_candidates() -> None:
    """Unknown states met at run time, with a suggested catalog entry each (review before adding to the profile)."""
    from interface_cua.evidence.catalog_mining import list_candidates

    _emit(list_candidates(load_settings().state_dir / "catalog-candidates"))


overrides_app = typer.Typer(no_args_is_help=True, help="Per-tenant override patches (locator-only, reviewed, hash-approved).")
app.add_typer(overrides_app, name="overrides")


@overrides_app.command("approve")
def overrides_approve(path: Path, approver: str = typer.Option(...), note: str = "") -> None:
    """Hash the patch content and bind an approval to that hash (any later edit voids it)."""
    from datetime import UTC, datetime

    from interface_cua.domain.artifacts import Approval
    from interface_cua.domain.overrides import OverridePatch

    patch = OverridePatch.model_validate_json(path.read_text(encoding="utf-8")).with_hash()
    at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    patch = patch.model_copy(update={"approval": Approval(approver=approver, at=at, content_hash=patch.content_hash, note=note)})
    path.write_text(canonical_json(patch.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
    _emit({"approved": patch.ref, "content_hash": patch.content_hash, "approver": approver})


@overrides_app.command("list")
def overrides_list(tenant: str = "tenant-b") -> None:
    from interface_cua.profiles.store import ProfileStore

    store = ProfileStore(load_settings())
    t = store.tenant(tenant)
    _emit(
        [
            {
                "ref": p.ref,
                "capability": p.capability_id,
                "versions": p.applies_to_versions,
                "approved": p.is_approved(),
                "ops": [f"{o.op} {o.step or o.output}" for o in p.ops],
            }
            for p in store.overrides(t)
        ]
    )


@llm_app.command("check")
def llm_check() -> None:
    """Show which planner/model discovery will use; for OpenAI-compatible providers, list the models the key can see."""
    from interface_cua.llm.factory import PlannerConfigError, make_planner, planner_kind

    try:
        planner = make_planner()
    except PlannerConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    info: dict[str, Any] = {"planner": planner_kind(), "model": planner.model, "prompt_template_hash": planner.prompt_template_hash}
    lister = getattr(planner, "list_models", None)
    if lister is not None:
        try:
            models = asyncio.run(lister())
            info["model_available"] = any(m.endswith(planner.model) for m in models)
            info["available_models"] = [m for m in models if "gemini" in m or "/" not in m][:40]
        except Exception as exc:
            typer.echo(f"could not list models: {exc}", err=True)
            raise typer.Exit(2) from None
    _emit(info)


def _kv(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs or []:
        if "=" not in p:
            raise typer.BadParameter(f"expected name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def _emit(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _target_url(settings: Settings, tenant: str = "tenant-a") -> str:
    from interface_cua.profiles.store import ProfileStore

    return ProfileStore(settings).tenant(tenant).base_url


# ------------------------------------------------------------------------------------------ target
@target_app.command("serve")
def target_serve(port: int = 8765, tenant: str = "tenant-a") -> None:
    """Run the synthetic target app in the foreground."""
    from target_app.server import serve

    serve(port=port, tenant=tenant)


@target_app.command("reset")
def target_reset(tenant: str = "tenant-a") -> None:
    from target_app import admin

    admin.reset(_target_url(load_settings(), tenant))
    typer.echo("target reset (seed data restored, faults cleared, ledger emptied)")


@target_app.command("ledger")
def target_ledger(tenant: str = "tenant-a") -> None:
    from target_app import admin

    _emit(admin.ledger(_target_url(load_settings(), tenant)))


@faults_app.command("set")
def faults_set(
    name: str,
    count: int = typer.Option(1, help="-1 = persistent"),
    param: list[str] = typer.Option(None, "--param"),
    tenant: str = "tenant-a",
) -> None:
    from target_app import admin

    params: dict[str, Any] = {k: (int(v) if v.isdigit() else v) for k, v in _kv(param).items()}
    _emit(admin.arm(_target_url(load_settings(), tenant), name, count=count, **params))


@faults_app.command("clear")
def faults_clear(tenant: str = "tenant-a") -> None:
    from target_app import admin

    admin.clear(_target_url(load_settings(), tenant))
    typer.echo("faults cleared")


# ------------------------------------------------------------------------------------------ discovery
@app.command()
def discover(
    request: Path = typer.Option(..., help="Discovery request JSON (goal, tenant, entry, declared inputs)."),
    example: list[str] = typer.Option(..., "--example", help="Value to use for a declared input during discovery, name=value."),
    fake: bool = typer.Option(False, help="Use the scripted offline planner (tests/demos without an API key; NOT genuine evidence)."),
    headed: bool = False,
    escalate: bool = typer.Option(False, help="On stuck, route an intervention to a human instead of failing."),
    version: str | None = typer.Option(None, help="Artifact version; default: 1.0.0, or the next free minor version."),
    no_compile: bool = False,
) -> None:
    """Run a genuine LLM-driven discovery, verify it, and compile a draft capability."""
    from interface_cua.discovery.compiler import compile_trajectory, surface_of
    from interface_cua.discovery.recorder import DiscoveryRequest
    from interface_cua.discovery.review import render_review
    from interface_cua.discovery.runner import run_discovery
    from interface_cua.profiles.store import ProfileStore
    from interface_cua.storage.capability_store import CapabilityStore

    settings = load_settings()
    req = DiscoveryRequest.model_validate_json(request.read_text(encoding="utf-8"))
    req = req.model_copy(update={"headed": headed or req.headed, "escalation_mode": "escalate" if escalate else req.escalation_mode})
    if fake:
        sys.path.insert(0, str(ROOT / "tests"))
        from helpers import balance_script
        from interface_cua.llm.fake_client import FakePlanner

        planner: Any = FakePlanner(balance_script)
    else:
        from interface_cua.llm.factory import PlannerConfigError, make_planner

        try:
            planner = make_planner()
        except PlannerConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from None

    async def go() -> Any:
        manager = server = None
        if req.escalation_mode == "escalate":
            manager, server = await _start_handoff(settings)
        try:
            return await run_discovery(settings, req, planner, _kv(example), escalator=manager)
        finally:
            if server:
                await server.stop()

    outcome = asyncio.run(go())
    result = outcome.result
    payload: dict[str, Any] = {"result": result.model_dump(mode="json", exclude_none=True), "run_dir": f"runs/{result.run_id}"}
    if result.status == "success" and outcome.trajectory is not None and not no_compile:
        tenant_profile = ProfileStore(settings).tenant(req.tenant_id)
        profile = ProfileStore(settings).app_profile(tenant_profile.app_profile)
        store = CapabilityStore(settings.capabilities_dir)
        if version is None:  # versions are immutable: a re-recording with the same contract is a minor bump
            existing = store.versions(req.capability_id)
            minor = max((int(v.split(".")[1]) for v in existing if v.startswith("1.")), default=-1) + 1
            version = f"1.{minor}.0"
        artifact = compile_trajectory(outcome.trajectory, profile, version=version, surface_type=surface_of(tenant_profile))
        path = store.save(artifact, render_review(artifact))
        payload["artifact"] = str(path.relative_to(ROOT))
        payload["artifact_status"] = artifact.lifecycle.status
        (settings.runs_dir / result.run_id / "artifact.json").write_text(
            canonical_json(artifact.model_dump(mode="json", exclude_none=True)), encoding="utf-8"
        )
    _emit(payload)
    raise typer.Exit(EXIT_CODES[result.status])


# ------------------------------------------------------------------------------------------ compile
@app.command("compile")
def compile_cmd(
    trajectory: Path | None = typer.Option(None, help="runs/<id>/trajectory.json from a verified discovery."),
    from_artifact: str | None = typer.Option(None, help="id@version to extend with an authored patch."),
    append: Path | None = typer.Option(None, help="Authored step patch JSON."),
    version: str = "1.0.0",
) -> None:
    """Compile a trajectory, or apply a reviewed authored patch to an existing version."""
    from interface_cua.discovery.compiler import AuthoredPatch, apply_patch, compile_trajectory, surface_of
    from interface_cua.discovery.recorder import Trajectory
    from interface_cua.discovery.review import render_review
    from interface_cua.profiles.store import ProfileStore
    from interface_cua.storage.capability_store import CapabilityStore

    settings = load_settings()
    store = CapabilityStore(settings.capabilities_dir)
    profiles = ProfileStore(settings)
    if trajectory is not None:
        traj = Trajectory.model_validate_json(trajectory.read_text(encoding="utf-8"))
        tenant_profile = profiles.tenant(traj.request.tenant_id)
        profile = profiles.app_profile(tenant_profile.app_profile)
        artifact = compile_trajectory(traj, profile, version=version, surface_type=surface_of(tenant_profile))
    elif from_artifact and append:
        cid, _, ver = from_artifact.partition("@")
        base = store.load(cid, ver or "latest")
        profile = profiles.app_profile(base.compatibility.app_profile)
        patch = AuthoredPatch.model_validate_json(append.read_text(encoding="utf-8"))
        artifact = apply_patch(base, patch, profile)
    else:
        raise typer.BadParameter("use --trajectory, or --from-artifact with --append")
    path = store.save(artifact, render_review(artifact))
    _emit(
        {
            "artifact": str(path.relative_to(ROOT)),
            "ref": artifact.ref,
            "content_hash": artifact.content_hash,
            "status": artifact.lifecycle.status,
            "side_effects": artifact.contract.side_effects,
        }
    )


# ------------------------------------------------------------------------------------------ validate / approve
@app.command()
def validate(
    capability_id: str,
    version: str = "latest",
    tenant: str = "tenant-a",
    good: list[str] = typer.Option(
        ..., "--good", help="Known-good input set, e.g. member_id=12345 (repeatable; comma-separate multiple fields)."
    ),
    negative: list[str] = typer.Option(
        None, "--negative", help="Negative input set and expected outcome: member_id=99999:member_not_found"
    ),
    expect_outputs: Path | None = typer.Option(
        None, help="JSON of expected outputs for the first good input (the verified discovery outputs)."
    ),
) -> None:
    """Replay a draft automatically (good + negative inputs); on success mark it validated."""
    from interface_cua.replay.engine import InvocationRequest, ReplayEngine
    from interface_cua.storage.capability_store import CapabilityStore

    settings = load_settings()
    store = CapabilityStore(settings.capabilities_dir)
    artifact = store.load(capability_id, version)
    irreversible = artifact.contract.side_effects == "irreversible"
    engine = ReplayEngine(settings)
    expected = json.loads(expect_outputs.read_text()) if expect_outputs else None
    report: dict[str, Any] = {
        "capability": artifact.ref,
        "content_hash": artifact.content_hash,
        "mode": "stop_before_irreversible" if irreversible else "full",
        "runs": [],
    }
    ok = True

    def inputs_of(spec: str) -> dict[str, str]:
        return _kv(spec.split(","))

    async def go() -> None:
        nonlocal ok
        for n, spec in enumerate(good):
            out = await engine.invoke(
                InvocationRequest(
                    capability_id=capability_id,
                    version=artifact.capability.version,
                    tenant_id=tenant,
                    inputs=inputs_of(spec),
                    mode="validation",
                    allow_unapproved=True,
                    stop_before_irreversible=irreversible,
                )
            )
            passed = out.result.status == "success" and (n > 0 or expected is None or out.outputs == expected)
            ok &= passed
            report["runs"].append(
                {
                    "kind": "good",
                    "run_id": out.result.run_id,
                    "status": out.result.status,
                    "passed": passed,
                    "outputs_match_discovery": (out.outputs == expected) if (n == 0 and expected is not None) else None,
                    "validation_stop": getattr(out.result, "validation_stop", None),
                }
            )
        for spec in negative or []:
            inp, _, want = spec.rpartition(":")
            out = await engine.invoke(
                InvocationRequest(
                    capability_id=capability_id,
                    version=artifact.capability.version,
                    tenant_id=tenant,
                    inputs=inputs_of(inp),
                    mode="validation",
                    allow_unapproved=True,
                    stop_before_irreversible=irreversible,
                )
            )
            passed = out.result.status == "business_outcome" and getattr(out.result, "code", None) == want
            ok &= passed
            report["runs"].append(
                {
                    "kind": "negative",
                    "expected_outcome": want,
                    "run_id": out.result.run_id,
                    "status": out.result.status,
                    "code": getattr(out.result, "code", None),
                    "passed": passed,
                }
            )

    asyncio.run(go())
    report["passed"] = ok
    if ok and artifact.lifecycle.status == "draft":
        artifact = store.transition(
            artifact,
            "validated",
            actor="cua validate",
            note="automatic validation replays passed",
            run_ids=[r["run_id"] for r in report["runs"]],
        )
        from interface_cua.discovery.review import render_review

        store.path(artifact.capability.id, artifact.capability.version).with_suffix(".review.md").write_text(
            render_review(artifact), encoding="utf-8"
        )
    report["status"] = artifact.lifecycle.status
    out_path = settings.runs_dir / f"validation-{artifact.capability.id}-{artifact.capability.version}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report"] = str(out_path.relative_to(ROOT))
    _emit(report)
    raise typer.Exit(0 if ok else 30)


@app.command()
def approve(capability_id: str, version: str = typer.Option(...), approver: str = typer.Option(...), note: str = "") -> None:
    """Human sign-off. Binds the approval to the artifact's content hash."""
    from interface_cua.discovery.review import render_review
    from interface_cua.storage.capability_store import CapabilityStore

    store = CapabilityStore(load_settings().capabilities_dir)
    artifact = store.load(capability_id, version)
    if artifact.lifecycle.status != "validated":
        typer.echo(f"{artifact.ref} is {artifact.lifecycle.status}; only validated artifacts can be approved", err=True)
        raise typer.Exit(2)
    artifact = store.transition(artifact, "approved", actor=approver, note=note)
    store.path(artifact.capability.id, artifact.capability.version).with_suffix(".review.md").write_text(
        render_review(artifact), encoding="utf-8"
    )
    _emit({"approved": artifact.ref, "content_hash": artifact.content_hash, "approver": approver})


# ------------------------------------------------------------------------------------------ replay
async def _start_handoff(settings: Settings) -> tuple[Any, Any]:
    from interface_cua.handoff.manager import InterventionManager
    from interface_cua.handoff.operator_api import OperatorServer

    manager = InterventionManager(settings)
    server = OperatorServer(manager, settings.operator_port)
    await server.start()
    typer.echo(f"operator API listening on http://127.0.0.1:{settings.operator_port}", err=True)
    return manager, server


@app.command()
def replay(
    capability_id: str,
    version: str = "approved-latest",
    tenant: str = "tenant-a",
    input: list[str] = typer.Option(None, "--input", help="name=value (repeatable)"),
    escalate: bool = typer.Option(
        False, help="escalation_mode=escalate: pause and hand the live session to a human when stuck/approval needed."
    ),
    headed: bool = False,
    allow_unapproved: bool = typer.Option(False, help="Development only; logged."),
    timeout: int = 180,
    caller: str = "cli",
    assisted_fallback: bool = typer.Option(
        False, help="Opt-in: if a recorded control is missing, ask the model once (bounded, policy-checked, logged)."
    ),
) -> None:
    """Deterministic replay of a saved capability (no LLM in the decision loop)."""
    from interface_cua.replay.engine import InvocationRequest, ReplayEngine

    settings = load_settings()
    req = InvocationRequest(
        capability_id=capability_id,
        version=version,
        tenant_id=tenant,
        inputs=_kv(input),
        escalation_mode="escalate" if escalate else "fail_fast",
        headed=headed,
        timeout_s=timeout,
        caller_id=caller,
        allow_unapproved=allow_unapproved,
        assisted_fallback=assisted_fallback,
    )
    advisor = None
    if assisted_fallback:
        from interface_cua.assist.fallback import LLMFallbackAdvisor
        from interface_cua.llm.factory import PlannerConfigError, make_planner

        try:
            advisor = LLMFallbackAdvisor(make_planner())
        except PlannerConfigError as exc:
            typer.echo(f"--assisted-fallback needs a model: {exc}", err=True)
            raise typer.Exit(2) from None

    async def go() -> Any:
        manager = server = None
        if escalate:
            manager, server = await _start_handoff(settings)
        try:
            return await ReplayEngine(settings, escalator=manager, fallback=advisor).invoke(req)
        finally:
            if server:
                await server.stop()

    out = asyncio.run(go())
    payload = out.result.model_dump(mode="json", exclude_none=True)
    if out.outputs:
        payload["outputs"] = out.outputs  # the caller receives real values; the persisted copy is masked
    _emit({"result": payload, "run_dir": f"runs/{out.result.run_id}"})
    raise typer.Exit(EXIT_CODES[out.result.status])


# ------------------------------------------------------------------------------------------ capabilities
@caps_app.command("list")
def caps_list() -> None:
    from interface_cua.storage.capability_store import CapabilityStore

    _emit(
        [
            {
                "ref": a.ref,
                "status": a.lifecycle.status,
                "approved": a.is_approved(),
                "side_effects": a.contract.side_effects,
                "hash": a.content_hash[:19],
            }
            for a in CapabilityStore(load_settings().capabilities_dir).list()
        ]
    )


@caps_app.command("show")
def caps_show(capability_id: str, version: str = "latest", review: bool = False) -> None:
    from interface_cua.discovery.review import render_review
    from interface_cua.storage.capability_store import CapabilityStore

    art = CapabilityStore(load_settings().capabilities_dir).load(capability_id, version)
    typer.echo(render_review(art) if review else canonical_json(art.model_dump(mode="json", exclude_none=True)))


@caps_app.command("tools")
def caps_tools() -> None:
    """Agent-facing catalog: approved capabilities as tool definitions (contract = JSON Schema)."""
    from interface_cua.agent_tools import tool_catalog

    _emit(tool_catalog(load_settings()))


# ------------------------------------------------------------------------------------------ operator
token_app = typer.Typer(no_args_is_help=True, help="Operator API tokens (only hashes are stored, in .cua/).")
op_app.add_typer(token_app, name="token")


def _directory() -> Any:
    from interface_cua.handoff.auth import OperatorDirectory

    s = load_settings()
    return OperatorDirectory(s.config_dir / "operators.json", s.state_dir / "operator_tokens.json")


@token_app.command("create")
def token_create(operator_id: str) -> None:
    """Issue a bearer token for an operator declared in config/operators.json (shown once)."""
    try:
        token = _directory().create_token(operator_id)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo(token)
    typer.echo(f"set CUA_OPERATOR_API_TOKEN to this value to act as {operator_id}; only its hash was stored", err=True)


@token_app.command("revoke")
def token_revoke(operator_id: str) -> None:
    _directory().revoke(operator_id)
    typer.echo(f"revoked all tokens of {operator_id}")


def _op_url(path: str) -> str:
    return f"http://127.0.0.1:{load_settings().operator_port}{path}"


def _op(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    import os

    api_token = os.environ.get("CUA_OPERATOR_API_TOKEN")
    if not api_token:
        typer.echo("set CUA_OPERATOR_API_TOKEN (issue one with `cua operator token create <operator-id>`)", err=True)
        raise typer.Exit(2)
    try:
        r = httpx.request(method, _op_url(path), json=body, timeout=30, headers={"authorization": f"Bearer {api_token}"})
    except httpx.ConnectError:
        typer.echo("no live run is serving the operator API (start a replay/discovery with --escalate)", err=True)
        raise typer.Exit(2) from None
    if r.status_code >= 400:
        typer.echo(f"error {r.status_code}: {r.text}", err=True)
        raise typer.Exit(2)
    return r.json()


@op_app.command("list")
def op_list() -> None:
    """Open interventions you may serve in the live run (falls back to the audit DB when no run is live)."""
    try:
        _emit(_op("GET", "/interventions"))
    except typer.Exit:
        from interface_cua.storage.run_store import AuditStore

        _emit(AuditStore(load_settings().db_path).list())


@op_app.command("status")
def op_status() -> None:
    """Who is in control of the live session, and who should be."""
    _emit(_op("GET", "/runs/current/control"))


@op_app.command("claim")
def op_claim(intervention_id: str) -> None:
    """Take control of the live session (identity comes from your API token)."""
    _emit(_op("POST", f"/interventions/{intervention_id}/claim", {}))


@op_app.command("act")
def op_act(
    intervention_id: str,
    token: int = typer.Option(..., help="lease token returned by claim"),
    kind: str = typer.Option(...),
    x: float | None = None,
    y: float | None = None,
    text: str | None = None,
    key: str | None = None,
) -> None:
    _emit(_op("POST", f"/interventions/{intervention_id}/act", {"token": token, "kind": kind, "x": x, "y": y, "text": text, "key": key}))


@op_app.command("controls")
def op_controls(intervention_id: str) -> None:
    _emit(_op("GET", f"/interventions/{intervention_id}/controls"))


@op_app.command("resolve")
def op_resolve(intervention_id: str, kind: str, token: int | None = None, note: str = "", step: str | None = None) -> None:
    """kind: resume | resume_at_step | complete | abort (operator) ; approve | deny (supervisor)"""
    body: dict[str, Any] = {"kind": kind, "token": token, "note": note, "step_id": step}
    if kind == "approve":
        body["action_hash"] = _op("GET", f"/interventions/{intervention_id}")["bound_action_hash"]
    _emit(_op("POST", f"/interventions/{intervention_id}/resolve", body))


# ------------------------------------------------------------------------------------------ evidence
@evidence_app.command("export")
def evidence_export(run_id: str, folder: str) -> None:
    """Copy a sanitized run into evidence/<folder>, gated by the leak scanner. Traces stay local-only."""
    from interface_cua.evidence.export import export_run

    settings = load_settings()
    dest = export_run(settings, run_id, folder)
    typer.echo(f"exported {run_id} -> {dest.relative_to(ROOT)}")
    if (settings.runs_dir / run_id / "trace.zip").exists():
        typer.echo(f"trace not exported (unmasked); it stays local-only at runs/{run_id}/trace.zip")


@app.command()
def stability(
    capability_id: str,
    runs: int = 10,
    tenant: str = "tenant-a",
    input: list[str] = typer.Option(None, "--input"),
    version: str = "approved-latest",
) -> None:
    """Replay N times and report pass rate and per-run duration spread."""
    from interface_cua.replay.engine import InvocationRequest, ReplayEngine

    settings = load_settings()
    engine = ReplayEngine(settings)

    async def go() -> list[Any]:
        return [
            await engine.invoke(InvocationRequest(capability_id=capability_id, version=version, tenant_id=tenant, inputs=_kv(input)))
            for _ in range(runs)
        ]

    outs = asyncio.run(go())
    durations = [o.result.duration_ms for o in outs]
    report = {
        "capability": outs[0].result.capability,
        "runs": runs,
        "passed": sum(o.result.status == "success" for o in outs),
        "statuses": [o.result.status for o in outs],
        "drift_signals": sum(len(getattr(o.result, "drift", [])) for o in outs),
        "recoveries": sum(len(getattr(o.result, "recoveries", [])) for o in outs),
        "duration_ms": {"min": min(durations), "max": max(durations), "mean": int(sum(durations) / len(durations))},
        "run_ids": [o.result.run_id for o in outs],
    }
    path = settings.runs_dir / "stability-report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _emit(report)
    raise typer.Exit(0 if report["passed"] == runs else 30)


if __name__ == "__main__":
    app()
