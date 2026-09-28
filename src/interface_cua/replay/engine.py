"""Deterministic replay: the production execution path. No model is consulted for any decision.

Pre-flight (no browser) -> session -> per step: location check, known-state scan, precondition,
unique locator resolution, policy on the bound action (approval for irreversible), act with the
lease token, wait on the postcondition while watching step-scoped outcomes and global states,
bounded idempotent-only recovery, extraction -> final success condition -> typed result.
Every exception is mapped to a result in exactly one place (``_map_exception``).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import jsonschema
from pydantic import BaseModel, ConfigDict, ValidationError

from interface_cua.config import Settings
from interface_cua.domain.actions import ActionKind, BoundAction, RiskClass
from interface_cua.domain.artifacts import CapabilityArtifact, Step
from interface_cua.domain.conditions import Condition, ValueRef
from interface_cua.domain.interventions import Intervention, InterventionKind
from interface_cua.domain.observations import Observation
from interface_cua.domain.overrides import OverrideError, apply_overrides
from interface_cua.domain.policy import PolicyLayer
from interface_cua.domain.profiles import CatalogState
from interface_cua.domain.results import (
    AbortedResult,
    BusinessOutcomeResult,
    DriftSignal,
    EvidenceRef,
    FailureResult,
    FieldError,
    InterventionSummary,
    LocatorDiagnostic,
    Recovery,
    RejectedResult,
    RunResult,
    SideEffectState,
    SuccessResult,
)
from interface_cua.domain.targets import LabelAnchorLocator, RoleNameLocator
from interface_cua.domain.types import NormalizationError, normalize
from interface_cua.domain.versions import version_in_range
from interface_cua.evidence.logger import new_run_id, now_iso
from interface_cua.handoff.control_lease import StaleLeaseError
from interface_cua.profiles.store import ProfileStore
from interface_cua.replay.resume import ResumeDecision, StepView, resolve_resume_point
from interface_cua.runtime import (
    RunEnv,
    SessionError,
    build_env,
    close_session,
    login,
    mine_unknown_state,
    navigate,
    scan_states,
    session_valid,
    state_message,
)
from interface_cua.storage.capability_store import ArtifactIntegrityError, CapabilityStore
from interface_cua.surfaces.base import ActionError, ResolutionError, Resolved


class InvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    version: str = "approved-latest"
    tenant_id: str
    inputs: dict[str, Any]
    escalation_mode: Literal["escalate", "fail_fast"] = "fail_fast"
    headed: bool = False
    timeout_s: int = 180
    caller_id: str = "cli"
    allow_unapproved: bool = False
    mode: Literal["replay", "validation"] = "replay"
    stop_before_irreversible: bool = False
    assisted_fallback: bool = False  # opt-in: one bounded, policy-checked model suggestion for a missing control


class Escalator(Protocol):
    async def escalate(self, env: RunEnv, **kwargs: Any) -> Intervention: ...

    def summary(self, intervention: Intervention) -> InterventionSummary: ...

    def human_performed_steps(self, intervention: Intervention, artifact: CapabilityArtifact) -> set[str]: ...


@dataclass
class FallbackSuggestion:
    handle: str | None
    rationale: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class FallbackAdvisor(Protocol):
    """Injected by the caller (outside ``replay/``). Replay never imports a model client itself."""

    async def suggest(self, *, step: Step, observation: Observation, screenshot: bytes, mask: Any) -> FallbackSuggestion: ...


class _NoModel:
    """Replay is constructed with this in place of a planner: any call is a bug, not a fallback."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("the LLM must never be consulted during deterministic replay")


# ---------------------------------------------------------------------------- internal signals
class Fail(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        expected: str | None = None,
        observed: str | None = None,
        diagnostics: list[LocatorDiagnostic] | None = None,
        escalate: InterventionKind | None = None,
    ):
        super().__init__(message)
        self.code, self.retryable, self.expected, self.observed = code, retryable, expected, observed
        self.diagnostics = diagnostics or []
        self.escalate = escalate


class Outcome(Exception):
    def __init__(self, code: str, message: str | None):
        super().__init__(code)
        self.code, self.message = code, message


class Reauth(Exception):
    pass


class Abort(Exception):
    def __init__(self, code: str, actor: str, reason: str):
        super().__init__(reason)
        self.code, self.actor, self.reason = code, actor, reason


class Complete(Exception):
    """A human completed the remaining steps; the engine still verifies success itself."""


class ValidationStop(Exception):
    pass


@dataclass
class ReplayOutcome:
    result: RunResult
    outputs: dict[str, Any] = field(default_factory=dict)  # real values for the caller only
    artifact: CapabilityArtifact | None = None


# ---------------------------------------------------------------------------- engine
class ReplayEngine:
    def __init__(self, settings: Settings, *, escalator: Escalator | None = None, fallback: FallbackAdvisor | None = None) -> None:
        self.settings = settings
        self.fallback = fallback
        self.store = CapabilityStore(settings.capabilities_dir)
        self.profiles = ProfileStore(settings)
        self.escalator = escalator
        self.llm = _NoModel()

    async def invoke(self, req: InvocationRequest) -> ReplayOutcome:
        return await _Run(self, req).execute()


class _Run:
    def __init__(self, engine: ReplayEngine, req: InvocationRequest) -> None:
        self.engine, self.req, self.settings = engine, req, engine.settings
        self.run_id = new_run_id("val" if req.mode == "validation" else "rep")
        self.started = time.monotonic()
        self.started_iso = now_iso()
        self.recoveries: list[Recovery] = []
        self.drift: list[DriftSignal] = []
        self.evidence: list[EvidenceRef] = []
        self.interventions: list[InterventionSummary] = []
        self.side_effects: SideEffectState = "none"
        self.outputs: dict[str, Any] = {}
        self.completed_by: dict[str, Literal["automation", "human"]] = {}
        self.last_checkpoint: str | None = None
        self.current_step: Step | None = None
        self.attempts = 1
        self.artifact: CapabilityArtifact | None = None
        self.overrides: list[str] = []
        self.fallback_used = False
        self.env: RunEnv | None = None

    # ------------------------------------------------------------ result helpers
    def _base(self) -> dict[str, Any]:
        a = self.artifact
        return {
            "run_id": self.run_id,
            "mode": self.req.mode,
            "capability": a.ref if a else f"{self.req.capability_id}@{self.req.version}",
            "content_hash": a.content_hash if a else None,
            "tenant_id": self.req.tenant_id,
            "started_at": self.started_iso,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "evidence": list(self.evidence),
            "interventions": list(self.interventions),
            "overrides": list(self.overrides),
        }

    def _reject(self, code: str, errors: list[FieldError]) -> RejectedResult:
        return RejectedResult(**self._base(), code=code, errors=errors)

    # ------------------------------------------------------------ pre-flight
    def _preflight(self) -> RejectedResult | None:
        req = self.req
        try:
            art = self.engine.store.load(
                req.capability_id, req.version if req.version != "approved-latest" or not req.allow_unapproved else "latest"
            )
        except FileNotFoundError as exc:
            return self._reject("artifact_invalid", [FieldError(field="capability", message=str(exc))])
        except (ArtifactIntegrityError, ValidationError, ValueError) as exc:
            return self._reject("artifact_invalid", [FieldError(field="artifact", message=str(exc)[:300])])
        self.artifact = art
        if req.mode == "replay" and not art.is_approved() and not req.allow_unapproved:
            return self._reject(
                "artifact_not_approved",
                [
                    FieldError(
                        field="lifecycle.status",
                        message=f"{art.ref} is {art.lifecycle.status}; unattended replay requires an approved, hash-bound artifact",
                    )
                ],
            )
        if art.lifecycle.status == "deprecated":
            return self._reject("artifact_not_approved", [FieldError(field="lifecycle.status", message="deprecated")])
        try:
            tenant = self.engine.profiles.tenant(req.tenant_id)
            profile = self.engine.profiles.app_profile(tenant.app_profile)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            return self._reject("artifact_invalid", [FieldError(field="tenant_id", message=str(exc))])
        if profile.id != art.target.app_profile or not version_in_range(profile.version, art.target.app_profile_version_range):
            return self._reject(
                "artifact_invalid",
                [
                    FieldError(
                        field="target.app_profile",
                        message=(
                            f"tenant runs {profile.id}@{profile.version}; "
                            f"artifact needs {art.target.app_profile} {art.target.app_profile_version_range}"
                        ),
                    )
                ],
            )
        # tenant specialization: reviewed, hash-approved override patches (locators only)
        try:
            effective, applied = apply_overrides(art, self.engine.profiles.overrides(tenant), tenant.tenant_id)
        except (OverrideError, ValidationError, ValueError, OSError) as exc:
            return self._reject("artifact_invalid", [FieldError(field="tenant.override_refs", message=str(exc)[:300])])
        self.artifact, self.overrides = effective, applied
        art = effective
        # bind inputs against the contract
        validator = jsonschema.Draft202012Validator(art.contract.inputs)
        errors = [FieldError(field=_schema_field(e), message=_safe_schema_message(e)) for e in validator.iter_errors(req.inputs)]
        if errors:
            return self._reject("input_contract_violation", errors)
        # effective policy (global ∩ tenant ∩ artifact) must permit what the artifact declares
        store = self.engine.profiles
        layers = [store.global_policy(), store.tenant_policy(tenant), self._artifact_layer(art)]
        for s in art.steps:
            for layer in layers:
                if layer.allowed_actions is not None and s.action not in layer.allowed_actions:
                    return self._reject(
                        "policy_denied_preflight",
                        [FieldError(field=f"steps.{s.id}", message=f"action {s.action.value} not permitted by {layer.id}")],
                    )
        irreversible = [s for s in art.steps if s.risk_class == RiskClass.IRREVERSIBLE]
        if irreversible and req.mode == "replay":
            if any(layer.irreversible_in_replay == "deny" for layer in layers):
                return self._reject(
                    "policy_denied_preflight",
                    [FieldError(field=f"steps.{irreversible[0].id}", message="irreversible actions are denied by policy")],
                )
            if req.escalation_mode == "fail_fast" or self.engine.escalator is None:
                return self._reject(
                    "policy_denied_preflight",
                    [
                        FieldError(
                            field=f"steps.{irreversible[0].id}",
                            message=(
                                "approval_required: this capability has an irreversible step that needs a bound human approval; "
                                "invoke with escalation_mode=escalate"
                            ),
                        )
                    ],
                )
        return None

    @staticmethod
    def _artifact_layer(art: CapabilityArtifact) -> PolicyLayer:
        return PolicyLayer(id=f"artifact:{art.ref}", allowed_routes=art.policy.allowed_routes, allowed_actions=art.policy.allowed_actions)

    # ------------------------------------------------------------ main
    async def execute(self) -> ReplayOutcome:
        rejected = self._preflight()
        if rejected is not None:
            return self._finish(rejected, log_only=True)
        art = self.artifact
        assert art is not None
        props = art.contract.inputs.get("properties", {})
        inputs = {k: (str(v), props.get(k, {}).get("x-sensitivity", "pii")) for k, v in self.req.inputs.items()}
        env = build_env(
            self.settings,
            run_id=self.run_id,
            mode=self.req.mode,
            tenant_id=self.req.tenant_id,
            inputs=inputs,
            extra_layers=[self._artifact_layer(art)],
            capability=art.ref,
            headed=self.req.headed,
        )
        self.env = env
        env.log.emit(
            "run_started",
            f"{self.req.mode} of {art.ref}",
            caller=self.req.caller_id,
            content_hash=art.content_hash,
            status=art.lifecycle.status,
            escalation_mode=self.req.escalation_mode,
            deterministic=True,
            inputs={k: env.redactor.text(str(v)) for k, v in self.req.inputs.items()},
        )
        result: RunResult
        budget = self.req.timeout_s
        if self.req.escalation_mode == "escalate":  # human time is bounded by the intervention SLAs, not the run timeout
            budget += 3 * (self.settings.limits.claim_sla_s + self.settings.limits.human_active_max_s)
        try:
            await asyncio.wait_for(self._run(art, env), timeout=budget)
            result = await self._success(art, env)
        except BaseException as exc:
            result = await self._map_exception(exc)
        finally:
            trace = await close_session(env)
            if trace:
                self.evidence.append(EvidenceRef(kind="trace", path="trace.zip", note="Playwright trace (synthetic tenant, post-login)"))
        result = result.model_copy(update={"evidence": [*self.evidence, EvidenceRef(kind="events", path="events.jsonl")]})
        return self._finish(result)

    def _finish(self, result: RunResult, *, log_only: bool = False) -> ReplayOutcome:
        env = self.env
        if env is None:  # rejected before a run directory existed: still leave a record
            run_dir = self.settings.runs_dir / self.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "run-result.json").write_text(result.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
            return ReplayOutcome(result, {}, self.artifact)
        persisted = result
        if isinstance(result, SuccessResult) and self.artifact:
            sens = {e.output: e.sensitivity for e in self.artifact.extract}
            persisted = result.model_copy(
                update={"outputs": {k: env.redactor.output_value(v, sens.get(k, "pii")) for k, v in result.outputs.items()}}
            )
        env.log.write_json("run-result.json", persisted.model_dump(mode="json", exclude_none=True))
        env.log.emit(
            "run_finished",
            f"{self.req.mode} finished: {result.status}" + (f" ({getattr(result, 'code', '')})" if hasattr(result, "code") else ""),
            status=result.status,
        )
        return ReplayOutcome(result, dict(self.outputs) if isinstance(result, SuccessResult) else {}, self.artifact)

    async def _run(self, art: CapabilityArtifact, env: RunEnv) -> None:
        from interface_cua.runtime import open_session

        await open_session(env, version_range=art.compatibility.version_range)
        await navigate(env, art.target.entry_point.route_handle)
        idx = 0
        while idx < len(art.steps):
            step = art.steps[idx]
            self.current_step = step
            try:
                await self._step(art, env, step)
                self.completed_by.setdefault(step.id, "automation")
                self.last_checkpoint = step.id
                idx = art.step_index(step.on_success) if step.on_success != "end" else len(art.steps)
            except Reauth:
                idx = await self._reauthenticate(art, env)
            except Fail as f:
                if f.escalate is None or self.req.escalation_mode != "escalate" or self.engine.escalator is None:
                    raise
                try:
                    idx = await self._escalate_and_resume(art, env, idx, f)
                except Reauth:
                    idx = await self._reauthenticate(art, env)

    # ------------------------------------------------------------ one step
    async def _step(self, art: CapabilityArtifact, env: RunEnv, step: Step) -> None:
        if self.req.mode == "validation" and self.req.stop_before_irreversible and step.risk_class == RiskClass.IRREVERSIBLE:
            await self._validation_stop(env, step)
        origin, route = await env.adapter.current_location()
        ok, _, why = env.policy.location_allowed(origin, route)
        if not ok:
            raise Fail("policy_violation", f"page outside allowlist before {step.id}: {why}", observed=route)
        await self._handle_states(env, step, phase="pre")
        resolved: Resolved | None = None
        if step.target is not None:
            if step.precondition is not None:
                res, _ = await env.adapter.wait_for(step.precondition, env.bindings, step.timeout_ms)
                if not res.holds:
                    await self._handle_states(env, step, phase="pre")
            resolved = await self._resolve(env, step)
            if await env.adapter.is_obscured(resolved):
                handled = await self._handle_states(env, step, phase="pre")
                if not handled or await env.adapter.is_obscured(resolved := await self._resolve(env, step)):
                    await self._evidence(env, f"{step.id}-obscured")
                    raise Fail(
                        "unknown_state",
                        f"'{step.target.description}' is covered by an unrecognized modal or disabled",
                        expected=f"{step.target.description} actionable",
                        observed="control obscured by an unknown overlay",
                        escalate="unknown_state",
                    )
        action = BoundAction(
            kind=step.action,
            target=step.target,
            value_ref=step.value,
            value=self._value(env, step.value),
            select_by=step.select_by,
            key=step.key,
            route_handle=step.route_handle,
        )
        action = await env.adapter.bind(action, resolved)
        origin, route = await env.adapter.current_location()
        decision = env.policy.evaluate(action, mode="replay", current_origin=origin, current_route=route)
        if RiskClass.max(decision.risk, step.risk_class) == RiskClass.IRREVERSIBLE and decision.verdict == "allow":
            decision = decision.model_copy(update={"verdict": "require_approval"})  # artifact risk can only raise
        env.log.emit(
            "policy_decision",
            f"{step.action.value} '{action.element_name or step.route_handle or ''}': {decision.verdict}",
            step_id=step.id,
            decision=decision,
        )
        if decision.verdict == "deny":
            raise Fail(
                "policy_violation",
                f"policy denied {step.id}: {decision.reasons[0]}",
                expected="allowed action",
                observed=decision.reasons[0],
            )
        if decision.verdict == "require_approval":
            await self._approval(art, env, step, action)
        pre_url = (await env.adapter.current_location())[1]
        if not step.idempotent:
            self.side_effects = "possible"
        t0 = time.monotonic()
        try:
            await env.adapter.perform(action, resolved, token=env.token(), actor="automation", timeout_ms=step.timeout_ms)
        except ActionError as exc:
            if await self._handle_states(env, step, phase="post"):
                resolved = await self._resolve(env, step) if step.target else None
                await env.adapter.perform(action, resolved, token=env.token(), actor="automation", timeout_ms=step.timeout_ms)
            else:
                raise Fail(
                    "timeout" if exc.code == "timeout" else "unknown_state",
                    f"{step.id} {step.action.value} failed: {exc}",
                    retryable=step.idempotent,
                    escalate="stuck",
                ) from exc
        env.log.emit(
            "action_executed",
            step.description,
            step_id=step.id,
            strategy=resolved.strategy if resolved else None,
            candidate_rank=resolved.candidate_index if resolved else None,
            value=action.value_ref,
            from_route=pre_url,
        )
        for d in env.adapter.drain_dialogs():
            if d.state_id:
                self._recover("dialog_handled", d.state_id, step.id, f"native {d.dialog_type} {d.response}ed per catalog")
            else:
                env.log.emit("state_detected", "unknown native dialog was dismissed", step_id=step.id, dialog=d)
        await self._await_postcondition(art, env, step, t0)
        if not step.idempotent:
            self.side_effects = "committed"
        await self._extract_after(art, env, step)

    async def _resolve(self, env: RunEnv, step: Step) -> Resolved:
        assert step.target is not None
        try:
            resolved = await env.adapter.resolve(step.target, env.bindings)
        except ResolutionError as exc:
            await self._evidence(env, f"{step.id}-{exc.code}")
            assisted = await self._assisted_fallback(env, step, exc)
            if assisted is not None:
                return assisted
            raise Fail(
                exc.code,
                f"{step.id}: {exc}",
                expected=step.target.description,
                observed="; ".join(f"{d.strategy}={d.match_count}" for d in exc.diagnostics),
                diagnostics=exc.diagnostics,
                escalate="locator_failure",
            ) from exc
        if resolved.candidate_index > 0:
            sig = DriftSignal(
                code="locator_fallback_used",
                step_id=step.id,
                detail=f"primary candidate failed; matched by {resolved.strategy} (rank {resolved.candidate_index})",
            )
            self.drift.append(sig)
            env.log.emit("drift", sig.detail, step_id=step.id, diagnostics=resolved.diagnostics)
        return resolved

    async def _assisted_fallback(self, env: RunEnv, step: Step, exc: ResolutionError) -> Resolved | None:
        """One bounded model suggestion when a recorded control cannot be found. The suggestion is
        guarded (same action, expected role, never irreversible, at most once per run), the normal policy
        check and postcondition still apply, and the new locator is saved as an *unapproved* override
        proposal for review - the artifact itself is never changed."""
        advisor = self.engine.fallback
        if (
            advisor is None
            or not self.req.assisted_fallback
            or self.fallback_used
            or exc.code != "locator_not_found"
            or step.risk_class == RiskClass.IRREVERSIBLE
            or step.target is None
            or step.action not in {ActionKind.CLICK, ActionKind.TYPE, ActionKind.SELECT}
        ):
            return None
        self.fallback_used = True
        obs, png = await env.adapter.observe(
            seq=0, save_to=env.run_dir / "screenshots" / f"{step.id}-assist.png", input_values=env.input_values
        )
        suggestion = await advisor.suggest(step=step, observation=obs, screenshot=png, mask=env.redactor.for_model)
        expected_roles = {c.role for c in step.target.candidates if isinstance(c, RoleNameLocator)} or {
            {"input": "textbox", "select": "combobox", "button": "button", "link": "link", "checkbox": "checkbox"}.get(c.control, "")
            for c in step.target.candidates
            if isinstance(c, LabelAnchorLocator)
        }
        control = obs.control(suggestion.handle) if suggestion.handle else None
        verdict = "accepted"
        if control is None:
            verdict = "rejected: no control suggested"
        elif expected_roles and control.role not in expected_roles:
            verdict = f"rejected: role {control.role} does not match the recorded step ({', '.join(sorted(expected_roles))})"
        env.log.emit(
            "note",
            f"assisted fallback for {step.id}: {verdict}",
            step_id=step.id,
            model=suggestion.model,
            input_tokens=suggestion.input_tokens,
            output_tokens=suggestion.output_tokens,
            suggestion={"handle": suggestion.handle, "name": control.name if control else None, "role": control.role if control else None},
            rationale=suggestion.rationale[:200],
        )
        if control is None or verdict != "accepted":
            return None
        resolved = env.adapter.handle(control.handle)
        assert resolved is not None
        proposal = await env.adapter.build_target(resolved, step.target.description, env.bindings)
        from interface_cua.domain.overrides import OverrideOp, OverridePatch

        patch = OverridePatch(
            id=f"{env.tenant.tenant_id}/{self.artifact.capability.id if self.artifact else '?'}/{step.id}-assisted",
            version="0.1.0",
            tenant_id=env.tenant.tenant_id,
            capability_id=self.artifact.capability.id if self.artifact else "?",
            applies_to_versions=f"=={self.artifact.capability.version}" if self.artifact else "",
            description=f"PROPOSED by assisted fallback ({suggestion.model}) in run {env.run_id}; needs review and approval before use.",
            author=f"assisted-fallback/{suggestion.model}",
            ops=[OverrideOp(op="prepend_candidate", step=step.id, candidates=proposal.candidates)],
        ).with_hash()
        env.log.write_json(f"proposed-override-{step.id}.json", patch.model_dump(mode="json", exclude_none=True))
        self._recover("assisted_fallback", None, step.id, f"model-suggested control '{control.name}' ({suggestion.model}); proposal saved")
        self.drift.append(DriftSignal(code="assisted_fallback", step_id=step.id, detail=f"recorded locator missing; used '{control.name}'"))
        return resolved

    def _value(self, env: RunEnv, ref: ValueRef | None) -> str | None:
        if ref is None:
            return None
        if ref.from_input is not None:
            return env.bindings.inputs[ref.from_input]
        if ref.vocab is not None:
            return env.bindings.vocab.get(ref.vocab, ref.vocab)
        return ref.literal

    # ------------------------------------------------------------ states & waits
    def _scoped(self, env: RunEnv, step: Step) -> dict[str, Any]:
        return {m.state: m for m in step.states}

    async def _handle_states(self, env: RunEnv, step: Step, *, phase: str) -> bool:
        """Scan global (+ step-scoped after the action) states and apply bounded handlers.
        Returns True if a recovery was applied. Raises for outcomes/failures/reauth."""
        applied = False
        for _ in range(3):
            scoped = list(self._scoped(env, step)) if phase == "post" else []
            matched = await scan_states(env, scoped)
            if not matched:
                return applied
            state = matched[0]
            env.log.emit("state_detected", f"catalog state {state.id} ({state.classification})", step_id=step.id, state_id=state.id)
            await self._apply_state(env, step, state)
            applied = True
        return applied

    async def _apply_state(self, env: RunEnv, step: Step, state: CatalogState) -> None:
        mapping = self._scoped(env, step).get(state.id)
        if mapping is not None and mapping.on == "business_outcome":
            await self._evidence(env, f"{step.id}-{state.id}")
            raise Outcome(mapping.outcome_code or state.id, await state_message(env, state))
        if mapping is not None and mapping.on == "fail":
            raise Fail(state.error_code or state.id, state.description)
        if state.classification == "hard_failure":
            await self._evidence(env, f"{step.id}-{state.id}")
            raise Fail(
                state.error_code or state.id,
                state.description,
                retryable=state.retryable,
                observed=await state_message(env, state),
                expected=f"{step.description} to proceed",
            )
        handler = state.handler
        if handler is None:
            raise Fail("unknown_state", f"{state.id} has no handler", escalate="unknown_state")
        if handler.kind == "reauthenticate":
            raise Reauth()
        if handler.kind == "dismiss" and handler.target is not None:
            resolved = await env.adapter.resolve(handler.target, env.bindings)
            action = await env.adapter.bind(BoundAction(kind=ActionKind.CLICK, target=handler.target), resolved)
            origin, route = await env.adapter.current_location()
            decision = env.policy.evaluate(action, mode="system", current_origin=origin, current_route=route)
            if not decision.allowed:
                raise Fail("policy_violation", f"recovery handler for {state.id} denied: {decision.reasons[0]}")
            await env.adapter.perform(action, resolved, token=env.token(), actor="automation")
            self._recover(state.recovery_code or "interstitial_dismissed", state.id, step.id, state.description)
            return
        if handler.kind == "retry_step":
            if not step.idempotent:
                await self._evidence(env, f"{step.id}-{state.id}")
                raise Fail(
                    state.error_code or state.id,
                    f"{state.description} on non-idempotent step {step.id}; not retried",
                    retryable=False,
                    observed=await state_message(env, state),
                )
            if self.attempts > handler.max_attempts:
                await self._evidence(env, f"{step.id}-{state.id}")
                raise Fail(
                    state.error_code or state.id,
                    f"{state.description}; still failing after {handler.max_attempts} retries",
                    retryable=True,
                    observed=await state_message(env, state),
                )
            await asyncio.sleep(handler.backoff_ms * self.attempts / 1000)  # bounded backoff between retries
            self.attempts += 1
            await env.adapter.reload(token=env.token())
            self._recover(state.recovery_code or "transient_retry", state.id, step.id, f"reloaded (attempt {self.attempts})")
            return
        raise Fail("unknown_state", f"unsupported handler {handler.kind} for {state.id}", escalate="unknown_state")

    async def _await_postcondition(self, art: CapabilityArtifact, env: RunEnv, step: Step, t0: float) -> None:
        """Wait for the postcondition while watching step-scoped outcomes and global states."""
        deadline = time.monotonic() + step.timeout_ms / 1000
        post = step.postcondition
        self.attempts = 1
        retried_timeout = False
        while True:
            if env.adapter.blocked_navigations:
                target = env.adapter.blocked_navigations.pop()
                await self._evidence(env, f"{step.id}-blocked-navigation")
                raise Fail(
                    "policy_violation",
                    f"{step.id} led to a navigation outside the allowlist; it was blocked",
                    expected=cond_text_safe(post),
                    observed=f"navigation to {target} blocked",
                )
            if post is None or (await env.adapter.evaluate(post, env.bindings)).holds:
                if post is not None:
                    await self._handle_states(env, step, phase="post") if step.states else None
                break
            if await self._handle_states(env, step, phase="post"):
                deadline = max(deadline, time.monotonic() + step.timeout_ms / 1000)
                continue
            if time.monotonic() > deadline:
                if step.idempotent and not retried_timeout and step.action in {ActionKind.CLICK, ActionKind.NAVIGATE}:
                    retried_timeout = True
                    try:
                        await env.adapter.reload(token=env.token())
                    except ActionError as exc:
                        raise Fail("timeout", f"{step.id}: reload for retry failed: {exc}", retryable=True) from exc
                    self._recover("transient_retry", None, step.id, "postcondition timed out; idempotent step reloaded once")
                    deadline = time.monotonic() + step.timeout_ms / 1000
                    continue
                await self._evidence(env, f"{step.id}-postcondition")
                from interface_cua.discovery.review import cond_text

                raise Fail(
                    "timeout",
                    f"{step.id}: expected state not reached within {step.timeout_ms} ms",
                    retryable=step.idempotent,
                    expected=cond_text(post),
                    observed=(await env.adapter.current_location())[1],
                    escalate="unknown_state",
                )
            await asyncio.sleep(0.15)
        elapsed = int((time.monotonic() - t0) * 1000)
        if elapsed > self.settings.limits.slow_threshold_ms:
            self._recover("slow_load_waited", None, step.id, f"postcondition reached after {elapsed} ms (explicit wait, no fixed sleep)")
        env.log.emit("checkpoint", f"{step.id} postcondition holds", step_id=step.id, elapsed_ms=elapsed)

    async def _extract_after(self, art: CapabilityArtifact, env: RunEnv, step: Step) -> None:
        for e in art.extract:
            if e.after_step != step.id:
                continue
            try:
                resolved = await env.adapter.resolve(e.target, env.bindings)
            except ResolutionError as exc:
                await self._evidence(env, f"extract-{e.output}")
                raise Fail(
                    exc.code,
                    f"output {e.output}: {exc}",
                    diagnostics=exc.diagnostics,
                    expected=e.target.description,
                    escalate="locator_failure",
                ) from exc
            raw = await env.adapter.read(resolved, e.method, e.attribute)
            if e.capture:
                m = re.search(e.capture, raw)
                if m is None:
                    await self._evidence(env, f"extract-{e.output}")
                    raise Fail(
                        "output_invalid",
                        f"output {e.output}: capture pattern did not match",
                        expected=e.capture,
                        observed=env.redactor.text(raw)[:80],
                    )
                raw = m.group(1)
            try:
                value = normalize(e.type, raw, enum_values=e.enum_values)
            except NormalizationError as exc:
                await self._evidence(env, f"extract-{e.output}")
                raise Fail(
                    "output_invalid",
                    f"output {e.output} is not a valid {e.type.value}",
                    expected=e.type.value,
                    observed=env.redactor.text(raw)[:80],
                ) from exc
            env.redactor.add_value(raw, e.sensitivity)
            self.outputs[e.output] = value
            env.log.emit(
                "extract",
                f"extracted {e.output} ({e.type.value})",
                step_id=step.id,
                output=e.output,
                value=env.redactor.output_value(value, e.sensitivity),
            )

    # ------------------------------------------------------------ recovery bookkeeping
    def _recover(self, code: str, state_id: str | None, step_id: str, detail: str) -> None:
        rec = Recovery(code=code, state_id=state_id, step_id=step_id, detail=detail)
        self.recoveries.append(rec)
        assert self.env is not None
        self.env.log.emit("recovery_applied", f"{code}" + (f" ({state_id})" if state_id else ""), step_id=step_id, recovery=rec)

    async def _evidence(self, env: RunEnv, reason: str) -> bool:
        try:
            self.evidence.extend(await env.adapter.capture_evidence(reason, env.run_dir, env.input_values))
            return True
        except Exception:
            return False

    async def _reauthenticate(self, art: CapabilityArtifact, env: RunEnv) -> int:
        if env.reauth_count >= 1:
            raise Fail("session_recovery_exhausted", "session expired again after one re-authentication", retryable=True)
        if self.side_effects != "none":
            raise Fail("session_recovery_exhausted", "session expired after a non-idempotent step; not re-entering", retryable=False)
        env.reauth_count += 1
        await login(env)
        await navigate(env, art.target.entry_point.route_handle)
        reentry = max(
            (i for i, s in enumerate(art.steps[: art.step_index(self.current_step.id) + 1 if self.current_step else 1]) if s.reentry_point),
            default=0,
        )
        self._recover(
            "session_reauthenticated",
            "session_expired",
            art.steps[reentry].id,
            f"re-authenticated via app-profile routine; re-entering at {art.steps[reentry].id}",
        )
        return reentry

    async def _validation_stop(self, env: RunEnv, step: Step) -> None:
        if step.precondition is not None:
            res, _ = await env.adapter.wait_for(step.precondition, env.bindings, step.timeout_ms)
            if not res.holds:
                raise Fail("checkpoint_mismatch", f"validation: precondition of irreversible step {step.id} does not hold")
        await self._resolve(env, step)
        env.log.emit(
            "validation_stop", f"stopped before irreversible step {step.id}; target resolves uniquely, precondition holds", step_id=step.id
        )
        raise ValidationStop(step.id)

    # ------------------------------------------------------------ approvals & escalation
    async def _approval(self, art: CapabilityArtifact, env: RunEnv, step: Step, action: BoundAction) -> None:
        if self.req.escalation_mode != "escalate" or self.engine.escalator is None:
            raise Fail("approval_required", f"{step.id} is irreversible and needs a human approval (fail_fast)")
        bound_hash = bound_action_hash(env, step, action)
        shot = await env.adapter.capture_evidence(f"{step.id}-approval", env.run_dir, env.input_values)
        self.evidence.extend(shot)
        iv = await self.engine.escalator.escalate(
            env,
            kind="approval_required",
            reason_code="approval_required",
            explanation=f"Irreversible step '{step.description}' requires a human decision before automation performs it.",
            capability=art.ref,
            step_id=step.id,
            step_description=step.description,
            bound_action=f"{step.action.value} '{action.element_name}' (risk {step.risk_class.value})",
            bound_action_hash=bound_hash,
            last_checkpoint_step=self.last_checkpoint,
            screenshot=shot[0].path if shot else None,
            permitted=["approve", "deny", "abort"],
        )
        self.interventions.append(self.engine.escalator.summary(iv))
        res = iv.resolution
        if iv.status == "expired" or res is None:
            raise Abort("intervention_timeout", "system", "approval not given within SLA")
        if res.kind == "abort":
            raise Abort("operator_abort", f"human:{res.operator_id}", res.note or "operator aborted")
        if res.kind == "deny":
            raise Fail("approval_denied", f"operator {res.operator_id} denied {step.id}: {res.note}")
        if res.kind != "approve" or res.action_hash != bound_hash:
            raise Fail("approval_denied", "approval does not match the exact bound action (hash mismatch)")
        if iv.resolved_at is None or time.time() - iv.resolved_at > iv.approval_valid_s:
            raise Fail("approval_denied", "approval expired before use")
        env.log.emit("note", f"approval {iv.id} consumed for {step.id} (single use, hash-bound)", step_id=step.id, action_hash=bound_hash)

    async def _escalate_and_resume(self, art: CapabilityArtifact, env: RunEnv, idx: int, f: Fail) -> int:
        escalator = self.engine.escalator
        assert escalator is not None and f.escalate is not None
        step = art.steps[idx]
        if f.code == "unknown_state":
            await mine_unknown_state(env, reason=str(f), step_id=step.id)
        for _round in range(3):
            shot = await env.adapter.capture_evidence(f"{step.id}-escalation", env.run_dir, env.input_values)
            self.evidence.extend(shot)
            iv = await escalator.escalate(
                env,
                kind=f.escalate,
                reason_code=f.code,
                explanation=str(f),
                capability=art.ref,
                step_id=step.id,
                step_description=step.description,
                expected=f.expected,
                observed=f.observed,
                last_checkpoint_step=self.last_checkpoint,
                screenshot=shot[0].path if shot else None,
                permitted=["resume", "resume_at_step", "complete", "abort"],
            )
            self.interventions.append(escalator.summary(iv))
            res = iv.resolution
            if iv.status == "expired" or res is None:
                raise Abort("intervention_timeout", "system", "no operator claimed the intervention within the SLA")
            if res.kind == "abort":
                raise Abort("operator_abort", f"human:{res.operator_id}", res.note or "operator aborted")
            human_done = escalator.human_performed_steps(iv, art)
            if not await session_valid(env):
                raise Reauth()
            if res.kind == "complete":
                for s in art.steps[idx:]:
                    self.completed_by[s.id] = "human"
                    if s.risk_class == RiskClass.IRREVERSIBLE:
                        self.side_effects = "committed" if s.id in human_done else "possible"
                raise Complete()
            if res.kind == "resume_at_step" and res.step_id:
                target_idx = art.step_index(res.step_id)
                env.log.emit("resume_point", f"operator chose to resume at {res.step_id}", step_id=res.step_id)
                return target_idx
            decision = await self._resume_point(art, env, idx, human_done)
            env.log.emit(
                "resume_point",
                f"resume decision: {decision.kind} at index {decision.index} ({decision.reason})",
                step_id=step.id,
                decision=decision.__dict__,
            )
            for sid in decision.human_completed:
                self.completed_by[sid] = "human"
            if decision.kind == "complete":
                raise Complete()
            if decision.kind in {"resume_at", "retry"}:
                for s in art.steps[idx : decision.index]:
                    await self._extract_after(art, env, s)
                return int(decision.index)
            f = Fail("unknown_state", f"after handback: {decision.reason}", escalate="unknown_state")
        raise Fail("unknown_state", "state still mismatched after repeated interventions")

    async def _resume_point(self, art: CapabilityArtifact, env: RunEnv, idx: int, human_done: set[str]) -> ResumeDecision:
        views = [StepView(s.id, s.risk_class == RiskClass.IRREVERSIBLE, s.postcondition is not None) for s in art.steps]
        post: dict[int, bool] = {}
        for k in range(idx, len(art.steps)):
            cond: Condition | None = art.steps[k].postcondition
            post[k] = bool(cond is not None and (await env.adapter.evaluate(cond, env.bindings)).holds)
        pre = art.steps[idx].precondition
        pre_ok = pre is None or (await env.adapter.evaluate(pre, env.bindings)).holds
        return resolve_resume_point(views, idx, post, pre_ok, human_done)

    # ------------------------------------------------------------ terminal results
    async def _success(self, art: CapabilityArtifact, env: RunEnv) -> RunResult:
        failures = []
        for c in art.success.checks:
            res, _ = await env.adapter.wait_for(c, env.bindings, 5000)
            if not res.holds:
                failures.append(res.detail)
        if failures:
            await self._evidence(env, "checkpoint-mismatch")
            return self._failure(
                Fail(
                    "checkpoint_mismatch",
                    "final success condition does not hold",
                    expected=art.success.description,
                    observed="; ".join(failures),
                )
            )
        for e in art.extract:  # outputs the human path may have skipped are read now
            if e.output not in self.outputs:
                await self._extract_after(art, env, art.step(e.after_step))
        schema_errors = [err.message for err in jsonschema.Draft202012Validator(art.contract.outputs).iter_errors(self.outputs)]
        if schema_errors:
            return self._failure(Fail("output_invalid", "outputs do not match the declared contract", observed=schema_errors[0][:200]))
        await self._evidence(env, "final-state")
        env.log.emit("checkpoint", "success condition verified", checks=len(art.success.checks))
        return SuccessResult(
            **self._base(),
            outputs=dict(self.outputs),
            checkpoint=art.success.description,
            recoveries=self.recoveries,
            drift=self.drift,
            completed_by=dict(self.completed_by),
        )

    def _failure(self, f: Fail) -> FailureResult:
        step = self.current_step
        return FailureResult(
            **self._base(),
            code=f.code,
            message=str(f),
            retryable=f.retryable and self.side_effects == "none",
            side_effect_state=self.side_effects,
            step_id=step.id if step else None,
            expected=f.expected,
            observed=f.observed,
            attempts=self.attempts,
            locator_diagnostics=f.diagnostics,
            recoveries=self.recoveries,
            drift=self.drift,
        )

    async def _map_exception(self, exc: BaseException) -> RunResult:
        """The one place internal exceptions become caller-facing results."""
        env = self.env
        if isinstance(exc, Complete | ValidationStop):
            if env is not None and isinstance(exc, ValidationStop):
                ok = await self._success_before_stop(env)
                if ok is not None:
                    return ok
            assert self.artifact is not None and env is not None
            return await self._success(self.artifact, env)
        if isinstance(exc, Outcome):
            return BusinessOutcomeResult(
                **self._base(),
                code=exc.code,
                details={"message": exc.message} if exc.message else {},
                step_id=self.current_step.id if self.current_step else None,
                message=exc.message,
                recoveries=self.recoveries,
            )
        if isinstance(exc, Fail):
            if exc.code == "unknown_state" and env is not None and exc.escalate is not None:
                await mine_unknown_state(env, reason=str(exc), step_id=self.current_step.id if self.current_step else None)
            return self._failure(exc)
        if isinstance(exc, Abort):
            return AbortedResult(
                **self._base(),
                code=exc.code,
                actor=exc.actor,
                reason=exc.reason,
                last_checkpoint_step=self.last_checkpoint,
                side_effect_state=self.side_effects,
            )
        if isinstance(exc, SessionError):
            return self._failure(Fail(exc.code, str(exc), retryable=exc.code in {"app_error"}))
        if isinstance(exc, asyncio.TimeoutError | TimeoutError):
            if env is not None:
                await self._evidence(env, "run-timeout")
            return self._failure(Fail("timeout", f"run exceeded {self.req.timeout_s}s", retryable=True))
        if isinstance(exc, StaleLeaseError):
            return self._failure(Fail("internal_error", f"control lease violation: {exc}"))
        if isinstance(exc, Reauth):
            return self._failure(Fail("session_recovery_exhausted", "session expired during handoff"))
        if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt):
            return AbortedResult(
                **self._base(),
                code="operator_abort",
                actor="system",
                reason="run cancelled",
                last_checkpoint_step=self.last_checkpoint,
                side_effect_state=self.side_effects,
            )
        if env is not None:
            await self._evidence(env, "internal-error")
            env.log.emit("note", f"internal error: {type(exc).__name__}: {exc}")
        return self._failure(Fail("internal_error", f"{type(exc).__name__}: {str(exc)[:300]}"))

    async def _success_before_stop(self, env: RunEnv) -> RunResult | None:
        """Validation of irreversible flows: everything up to the commit ran and verified."""
        assert self.artifact is not None and self.current_step is not None
        return SuccessResult(
            **self._base(),
            outputs=dict(self.outputs),
            checkpoint=f"validated up to (not including) irreversible step {self.current_step.id}",
            recoveries=self.recoveries,
            drift=self.drift,
            completed_by=dict(self.completed_by),
            validation_stop=self.current_step.id,
        )


def cond_text_safe(cond: Condition | None) -> str:
    from interface_cua.discovery.review import cond_text

    return cond_text(cond)


def bound_action_hash(env: RunEnv, step: Step, action: BoundAction) -> str:
    """Approval binding: run + session + step + the exact bound action (incl. keyed value hash)."""
    value_h = env.redactor.hash8(action.value) if action.value else ""
    material = "|".join([env.run_id, env.adapter.session_id, step.id, action.fingerprint(), value_h])
    return "sha256:" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _schema_field(e: jsonschema.ValidationError) -> str:
    if e.absolute_path:
        return ".".join(str(p) for p in e.absolute_path)
    if e.validator == "required" and "'" in str(e.message):
        return str(e.message).split("'")[1]
    return "inputs"


def _safe_schema_message(e: jsonschema.ValidationError) -> str:
    """Schema messages echo the offending value; keep them free of the (possibly sensitive) input."""
    if e.validator == "pattern":
        return f"does not match pattern {e.validator_value}"
    if e.validator == "enum":
        return f"must be one of {e.validator_value}"
    if e.validator == "required":
        return "is required"
    if e.validator == "additionalProperties":
        return "unexpected input field(s)"
    if e.validator == "type":
        return f"must be of type {e.validator_value}"
    return e.validator if isinstance(e.validator, str) else "invalid"
