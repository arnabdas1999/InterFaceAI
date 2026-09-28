"""Run orchestration shared by discovery, validation, and replay: builds the policy/redaction/lease
stack for one run, opens the live session, runs the app-profile login routine, checks the app
fingerprint, and scans the state catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from interface_cua.config import Settings, hmac_key
from interface_cua.domain.actions import ActionKind, BoundAction
from interface_cua.domain.conditions import Condition, DialogPresent
from interface_cua.domain.interventions import LeaseSnapshot
from interface_cua.domain.policy import PolicyLayer
from interface_cua.domain.profiles import AppProfile, CatalogState, TenantProfile
from interface_cua.domain.routes import canonicalize
from interface_cua.domain.versions import version_in_range
from interface_cua.evidence.logger import RunLog
from interface_cua.handoff.control_lease import ControlLease
from interface_cua.policy.engine import PolicyEngine
from interface_cua.policy.redaction import Redactor
from interface_cua.profiles.store import ProfileStore
from interface_cua.secrets.provider import EnvSecretProvider
from interface_cua.surfaces.base import ActionError, Bindings, ResolutionError
from interface_cua.surfaces.browser.adapter import BrowserAdapter

RunMode = Literal["discovery", "replay", "validation"]


class SessionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class RunEnv:
    settings: Settings
    run_id: str
    mode: RunMode
    profile: AppProfile
    tenant: TenantProfile
    policy: PolicyEngine
    redactor: Redactor
    log: RunLog
    lease: ControlLease
    adapter: BrowserAdapter
    bindings: Bindings
    secrets: EnvSecretProvider
    reauth_count: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def run_dir(self) -> Path:
        return self.log.run_dir

    @property
    def input_values(self) -> dict[str, str]:
        return self.bindings.inputs

    def token(self) -> int:
        return self.lease.version


def build_env(
    settings: Settings,
    *,
    run_id: str,
    mode: RunMode,
    tenant_id: str,
    inputs: dict[str, tuple[str, str]],
    extra_layers: list[PolicyLayer] | None = None,
    capability: str | None = None,
    headed: bool = False,
) -> RunEnv:
    """``inputs``: name -> (value, sensitivity)."""
    store = ProfileStore(settings)
    tenant = store.tenant(tenant_id)
    profile = store.app_profile(tenant.app_profile)
    layers = [store.global_policy(), store.tenant_policy(tenant), *(extra_layers or [])]
    policy = PolicyEngine(layers, profile)
    secrets = EnvSecretProvider()
    redactor = Redactor(
        hmac_key(settings),
        sensitive_map=profile.sensitive_fields,
        inputs=inputs,
        secrets=secrets.all_values(tenant.secret_ref),
    )
    run_dir = settings.runs_dir / run_id
    log = RunLog(run_dir, run_id, mode, redactor, capability=capability)
    lease = ControlLease(session_id="pending")
    log.bind_control(lambda: (lease.owner, lease.version))

    def on_lease(old: LeaseSnapshot, new: LeaseSnapshot, reason: str) -> None:
        log.emit(
            "lease_transition",
            f"control {old.state} -> {new.state} ({reason})",
            **{"from": old.state.value, "to": new.state.value},
            owner=new.owner,
            expected_owner=new.expected_owner,
            version=new.version,
        )

    lease.set_listener(on_lease)
    adapter_kwargs: dict[str, Any] = {
        "profile": profile,
        "policy": policy,
        "redactor": redactor,
        "log": log,
        "lease": lease,
        "base_url": tenant.base_url,
        "headed": headed,
        "trace": tenant.data_classification == "synthetic",  # traces are unredacted: synthetic only
    }
    adapter: BrowserAdapter
    if tenant.surface == "legacy-web":
        from interface_cua.surfaces.browser.legacy import LegacyWebAdapter

        adapter = LegacyWebAdapter(root_frame=tenant.root_frame or "content", **adapter_kwargs)
    elif tenant.surface == "desktop":
        from interface_cua.surfaces.desktop.adapter import DesktopAdapter

        # Same surface contract, structurally (not a BrowserAdapter subclass).
        adapter = DesktopAdapter(tenant=tenant, **adapter_kwargs)  # type: ignore[assignment]
    else:
        adapter = BrowserAdapter(**adapter_kwargs)
    bindings = Bindings(
        inputs={k: v for k, (v, _) in inputs.items()},
        vocab=tenant.vocab(profile),
        base_url=tenant.base_url,
    )
    env = RunEnv(
        settings=settings,
        run_id=run_id,
        mode=mode,
        profile=profile,
        tenant=tenant,
        policy=policy,
        redactor=redactor,
        log=log,
        lease=lease,
        adapter=adapter,
        bindings=bindings,
        secrets=secrets,
    )
    adapter.dialog_decider = make_dialog_decider(env)
    return env


# ---------------------------------------------------------------------------- dialogs
def dialog_states(profile: AppProfile) -> list[CatalogState]:
    return [s for s in profile.state_catalog if isinstance(s.detector, DialogPresent)]


def make_dialog_decider(env: RunEnv):  # type: ignore[no-untyped-def]
    """Native dialogs must be answered immediately (the page is blocked). Known dialogs get their
    catalogued response after a policy check; unknown ones are dismissed (the non-committing
    choice) and the resulting state is left for the step's postcondition/escalation logic."""

    def decide(dtype: str, message: str) -> tuple[str, str, str | None]:
        for state in dialog_states(env.profile):
            det = state.detector
            assert isinstance(det, DialogPresent)
            if det.dialog_type in ("any", dtype) and re.search(det.text_pattern, message, re.IGNORECASE):
                response = state.handler.dialog_response if state.handler and state.handler.dialog_response else "dismiss"
                action = BoundAction(kind=ActionKind.DIALOG_RESPOND, dialog_response=response)
                page = env.adapter.page
                where = canonicalize(page.url) if page is not None else canonicalize(env.tenant.base_url)
                decision = env.policy.evaluate(
                    action,
                    mode="system",
                    current_origin=where.origin,
                    current_route=where.path,
                    dialog_text=message,
                    dialog_known_safe=True,
                )
                if decision.allowed:
                    return response, f"catalog state {state.id}", state.id
                return "dismiss", f"policy denied {response}: {decision.reasons[0]}", state.id
        return "dismiss", "unknown dialog: dismissed (never auto-accepted)", None

    return decide


# ---------------------------------------------------------------------------- session
async def navigate(env: RunEnv, route_handle: str, *, actor: str = "automation") -> None:
    origin, route = (env.tenant.base_url, "/")
    if env.adapter.page is not None and env.adapter.page.url.startswith("http"):
        origin, route = await env.adapter.current_location()
    action = BoundAction(kind=ActionKind.NAVIGATE, route_handle=route_handle)
    decision = env.policy.evaluate(action, mode="system", current_origin=env.tenant.base_url, current_route=route)
    env.log.emit("policy_decision", f"navigate {route_handle}: {decision.verdict}", decision=decision)
    if not decision.allowed:
        raise SessionError("policy_violation", f"navigation to {route_handle} denied: {decision.reasons[0]}")
    await env.adapter.perform(action, None, token=env.token(), actor=actor)
    _ = origin


async def login(env: RunEnv) -> None:
    """Deterministic app-profile login. The model never sees or types credentials."""
    routine = env.profile.login
    if routine is None:
        return  # surface without sign-on
    page = env.adapter.page
    assert page is not None
    ok, reason = env.policy.url_allowed(env.tenant.base_url + routine.route)
    if not ok:
        raise SessionError("policy_violation", f"login route not allowed: {reason}")
    await page.goto(env.tenant.base_url + routine.route, wait_until="domcontentloaded")
    for step in routine.steps:
        try:
            resolved = await env.adapter.resolve(step.target, env.bindings)
        except ResolutionError as exc:
            raise SessionError("app_incompatible", f"login form: {exc}") from exc
        if step.action == "type":
            assert step.secret_field is not None
            try:
                secret = env.secrets.get(routine.secret_ref, step.secret_field)
            except KeyError as exc:
                raise SessionError("internal_error", f"credential not configured: {exc.args[0]}") from exc
            await env.adapter.type_secret(resolved, secret.reveal(), token=env.token())
        else:
            try:
                await env.adapter.perform(BoundAction(kind=ActionKind.CLICK), resolved, token=env.token(), actor="automation")
            except ActionError as exc:
                raise SessionError("app_error", f"login submit failed: {exc}") from exc
    result, _ = await env.adapter.wait_for(routine.success, env.bindings, 10_000)
    if not result.holds:
        raise SessionError("session_recovery_exhausted", "login did not reach the post-login page")
    env.log.emit("note", "authenticated via app-profile login routine (credentials from secret provider)")


async def check_fingerprint(env: RunEnv, version_range: str | None = None) -> str:
    fp = env.profile.fingerprint
    generator = await env.adapter.generator() or ""
    m = re.search(fp.generator_pattern, generator)
    if not m:
        raise SessionError("app_incompatible", f"application fingerprint mismatch (generator {generator!r})")
    version = m.group(1)
    rng = version_range or fp.version_range
    if not version_in_range(version, rng):
        raise SessionError("app_incompatible", f"{fp.product} {version} outside supported range {rng}")
    env.log.emit("note", f"fingerprint ok: {fp.product} {version} within {rng}", product=fp.product, version=version)
    return version


async def open_session(env: RunEnv, *, version_range: str | None = None) -> None:
    await env.adapter.start()
    env.lease._snap = env.lease.snapshot.model_copy(update={"session_id": env.adapter.session_id})
    await login(env)
    await check_fingerprint(env, version_range)
    await env.adapter.start_trace()


async def close_session(env: RunEnv) -> Path | None:
    trace_path = env.run_dir / "trace.zip" if env.adapter.trace_enabled else None
    await env.adapter.close(trace_path)
    return trace_path if trace_path and trace_path.exists() else None


async def session_valid(env: RunEnv) -> bool:
    if env.profile.login is None:
        return True
    return (await env.adapter.evaluate(env.profile.login.session_valid, env.bindings)).holds


def settle_condition(env: RunEnv) -> Condition:
    """Cheap condition used to let a screen settle after an action."""
    from interface_cua.domain.conditions import FrameLoaded

    return env.profile.login.session_valid if env.profile.login else FrameLoaded(frame_path=[])


# ---------------------------------------------------------------------------- catalog
async def scan_states(env: RunEnv, state_ids: list[str] | None = None, *, include_global: bool = True) -> list[CatalogState]:
    """Evaluate catalog detectors (global + the given step-scoped ids). Dialog states are
    event-driven and handled by the dialog decider, so they are excluded here."""
    matched: list[CatalogState] = []
    wanted = set(state_ids or [])
    for state in env.profile.state_catalog:
        if isinstance(state.detector, DialogPresent):
            continue
        if not ((include_global and state.scope == "global") or state.id in wanted):
            continue
        result = await env.adapter.evaluate(state.detector, env.bindings)
        if result.holds:
            matched.append(state)
    return matched


async def state_message(env: RunEnv, state: CatalogState) -> str | None:
    """Sanitized message for outcomes/failures: the region's text, or for whole-page regions only
    the lines that matched the detector (never a dump of the page)."""
    if state.message_region is None:
        return None
    from interface_cua.domain.conditions import TextInRegion

    if state.message_region.any_frame and isinstance(state.detector, TextInRegion) and state.detector.pattern:
        lines: list[str] = []
        for text in await env.adapter.page_texts():
            lines += [ln.strip() for ln in text.splitlines() if re.search(state.detector.pattern, ln, re.IGNORECASE)]
        return env.redactor.text(" | ".join(lines))[:300] or None
    text = await env.adapter._region_text(TextInRegion(region=state.message_region), env.bindings)
    return env.redactor.text(text)[:300] if text else None


async def mine_unknown_state(env: RunEnv, *, reason: str, step_id: str | None) -> None:
    """Record the unrecognized state as a reviewable catalog candidate (never auto-added)."""
    from interface_cua.domain.routes import generalize_path
    from interface_cua.evidence.catalog_mining import record_candidate

    try:
        sig = await env.adapter.state_signature()
        sig = {k: (env.redactor.text(v) if isinstance(v, str) else [env.redactor.text(x) for x in v] if isinstance(v, list) else v)
               for k, v in sig.items()}  # fmt: skip
        _, route = await env.adapter.current_location()
        path = record_candidate(
            env.settings.state_dir / "catalog-candidates",
            sig,
            route=generalize_path(route, env.bindings.inputs),
            run_id=env.run_id,
            step_id=step_id,
            reason=reason,
            app_profile=f"{env.profile.id}@{env.profile.version}",
        )
        env.log.emit("note", f"catalog candidate recorded: '{sig.get('title')}'", step_id=step_id, candidate=path.stem)
    except Exception as exc:
        env.log.emit("note", f"catalog mining skipped: {type(exc).__name__}", step_id=step_id)
