"""LLM-driven discovery: observe -> decide -> validate -> resolve -> policy -> act -> record.

The model locates and decides; deterministic code resolves handles, substitutes values, enforces
policy, reads values, and independently verifies a ``done`` claim before anything is compiled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from interface_cua.config import Settings
from interface_cua.discovery.recorder import (
    DiscoveryRequest,
    ExtractRecord,
    FrameState,
    OutputDecl,
    RecordedStep,
    Trajectory,
)
from interface_cua.domain.actions import ActionKind, BoundAction, RiskClass
from interface_cua.domain.conditions import TextInRegion, ValueRef
from interface_cua.domain.observations import Observation
from interface_cua.domain.results import (
    AbortedResult,
    EvidenceRef,
    FailureResult,
    InterventionSummary,
    RunResult,
    SuccessResult,
    Usage,
)
from interface_cua.domain.routes import generalize_path
from interface_cua.domain.targets import FrameRef, LabelAnchorLocator, RegionSpec, TargetSpec
from interface_cua.domain.types import NormalizationError, normalize
from interface_cua.evidence.logger import new_run_id, now_iso
from interface_cua.llm.client import DeclaredInput, DiscoveryContext, DiscoveryDecision, LLMClient
from interface_cua.runtime import (
    SessionError,
    build_env,
    close_session,
    navigate,
    open_session,
    scan_states,
    settle_condition,
)
from interface_cua.surfaces.base import ActionError, ResolutionError, Resolved

KIND_MAP = {
    "click": ActionKind.CLICK,
    "type": ActionKind.TYPE,
    "select": ActionKind.SELECT,
    "press": ActionKind.PRESS,
    "navigate": ActionKind.NAVIGATE,
}


class Stop(Exception):
    def __init__(self, code: str, message: str, *, escalate: bool = False):
        super().__init__(message)
        self.code = code
        self.escalate = escalate


@dataclass
class DiscoveryOutcome:
    result: RunResult
    trajectory: Trajectory | None
    outputs: dict[str, Any] = field(default_factory=dict)  # real values, returned to the caller only


def render_observation(obs: Observation, extracted: list[str], mask: Any = None) -> str:
    """Model-facing text. Routes are masked too: a path like /members/12345 carries the input value."""
    m = mask or (lambda s: s)
    lines = [f"Main route: {m(obs.route)}   Title: {obs.title}"]
    for f in obs.frames:
        where = "main page" if f.index == 0 else f"frame {f.index} (name={f.name}) route {m(f.route)}"
        lines.append(f"[frame {f.index}] {where}")
    lines.append("CONTROLS (number | role | name | extra):")
    for c in obs.controls:
        extra = []
        if c.frame_index:
            extra.append(f"frame {c.frame_index}")
        if c.value:
            extra.append(f"value={c.value}")
        if c.options:
            extra.append("options=" + "/".join(c.options[:12]))
        if not c.enabled:
            extra.append("DISABLED-or-OBSCURED")
        if c.checked is not None:
            extra.append(f"checked={c.checked}")
        if c.name_source == "adjacent_cell":
            extra.append("label from adjacent cell")
        lines.append(f"  {c.handle} | {c.role} | {c.name!r} | {', '.join(extra)}")
    for idx, text in obs.text.items():
        lines.append(f"TEXT frame {idx}:\n{text[:2500]}")
    for d in obs.dialogs:
        lines.append(f"NATIVE DIALOG ({d.dialog_type}) '{d.message}' was {d.response}ed by the system: {d.reason}")
    for b in obs.blocked:
        lines.append(f"BLOCKED by policy: {b}")
    return "\n".join(lines)


class DiscoveryRunner:
    def __init__(
        self,
        settings: Settings,
        request: DiscoveryRequest,
        planner: LLMClient,
        *,
        escalator: Any = None,
        example_inputs: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.req = request
        self.planner = planner
        self.escalator = escalator
        self.examples = example_inputs or {}
        self.run_id = new_run_id("disc")
        self.steps: list[RecordedStep] = []
        self.history: list[str] = []
        self.extracts: dict[str, ExtractRecord] = {}
        self.extracted_values: dict[str, Any] = {}
        self.usage = {"decisions": 0, "input": 0, "output": 0, "latency": 0}
        self.observed_states: list[str] = []
        self.interventions: list[InterventionSummary] = []
        self.evidence: list[EvidenceRef] = []
        self.extract_after: dict[str, int] = {}
        self.trajectory: Trajectory | None = None
        self.denied_keys: set[str] = set()

    # ---------------------------------------------------------------- helpers
    def _gen(self, path: str) -> str:
        return generalize_path(path, self.env.bindings.inputs)

    def _gen_target(self, t: TargetSpec) -> TargetSpec:
        fp = [FrameRef(name=f.name, route=self._gen(f.route) if f.route else None) for f in t.frame_path]
        return t.model_copy(update={"frame_path": fp})

    def _frames_state(self, obs: Observation) -> list[FrameState]:
        return [
            FrameState(
                frame_path=[FrameRef(name=p.name, route=None if p.name else self._gen(p.route or "")) for p in f.path],
                route=self._gen(f.route),
            )
            for f in obs.frames
            if f.index > 0
        ]

    def _declared(self) -> list[DeclaredInput]:
        return [
            DeclaredInput(name=i.name, type=i.type, description=i.description, sensitivity=i.sensitivity, enum=i.enum)
            for i in self.req.inputs
        ]

    # ---------------------------------------------------------------- main
    async def run(self) -> DiscoveryOutcome:
        inputs: dict[str, tuple[str, str]] = {i.name: (self.examples[i.name], str(i.sensitivity)) for i in self.req.inputs}
        self.env = build_env(
            self.settings,
            run_id=self.run_id,
            mode="discovery",
            tenant_id=self.req.tenant_id,
            inputs=inputs,
            capability=self.req.capability_id,
            headed=self.req.headed,
        )
        env = self.env
        self.started = time.monotonic()
        self.started_iso = now_iso()
        env.log.emit(
            "run_started",
            "discovery run started",
            goal=self.req.goal,
            tenant=self.req.tenant_id,
            entry=self.req.entry,
            model=self.planner.model,
            prompt_template_hash=self.planner.prompt_template_hash,
            inputs=[i.model_dump() for i in self.req.inputs],
            limits={
                "max_steps": self.req.max_steps,
                "timeout_s": self.req.timeout_s,
                "max_run_tokens": self.settings.limits.max_run_tokens,
            },
        )
        outcome: DiscoveryOutcome
        try:
            await open_session(env)
            await navigate(env, self.req.entry)
            outcome = await self._loop()
        except SessionError as exc:
            outcome = DiscoveryOutcome(self._failure(exc.code, str(exc)), None)
        except Stop as exc:
            outcome = DiscoveryOutcome(self._failure(exc.code, str(exc)), None)
        finally:
            trace = await close_session(env)
            if trace:
                self.evidence.append(EvidenceRef(kind="trace", path="trace.zip", note="Playwright trace (synthetic tenant, post-login)"))
        result = outcome.result.model_copy(update={"evidence": [*self.evidence, EvidenceRef(kind="events", path="events.jsonl")]})
        outcome.result = result
        persisted = result
        if isinstance(result, SuccessResult):
            persisted = result.model_copy(update={"outputs": self._persisted_outputs(result.outputs)})
        env.log.write_json("run-result.json", persisted.model_dump(mode="json", exclude_none=True))
        env.log.emit("run_finished", f"discovery finished: {result.status}", status=result.status)
        return outcome

    def _persisted_outputs(self, outputs: dict[str, Any]) -> dict[str, Any]:
        return {k: self.env.redactor.output_value(v, "financial" if self._type_of(k) == "money" else "pii") for k, v in outputs.items()}

    def _type_of(self, name: str) -> str:
        rec = self.extracts.get(name)
        return rec.output_type.value if rec else "string"

    def _base(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": "discovery",
            "capability": self.req.capability_id,
            "tenant_id": self.req.tenant_id,
            "started_at": self.started_iso,
            "finished_at": now_iso(),
            "duration_ms": int((time.monotonic() - self.started) * 1000),
            "interventions": self.interventions,
            "usage": Usage(
                model=self.planner.model,
                decisions=self.usage["decisions"],
                input_tokens=self.usage["input"],
                output_tokens=self.usage["output"],
                latency_ms=self.usage["latency"],
            ),
        }

    def _failure(self, code: str, message: str) -> FailureResult:
        return FailureResult(
            **self._base(),
            code=code,
            message=message,
            retryable=code in {"timeout", "app_error"},
            side_effect_state="none",
            step_id=f"d{len(self.steps)}",
        )

    async def _loop(self) -> DiscoveryOutcome:
        env = self.env
        feedback: str | None = None
        invalid = 0
        no_progress = 0
        denied: dict[str, int] = {}
        seen: set[str] = set()
        prev_fp = ""
        # Only UI actions (and waits) are expected to change the screen; extract/done/invalid turns are
        # read-only, so they must not count toward the no-progress stop condition.
        last_changes_ui = True
        for step in range(1, self.req.max_steps + 1):
            if time.monotonic() - self.started > self.req.timeout_s:
                return await self._stuck("timeout", "wall-clock limit reached", feedback)
            if self.usage["input"] + self.usage["output"] > self.settings.limits.max_run_tokens:
                return await self._stuck("discovery_exhausted", "token budget exhausted", feedback)
            recovered = await self._handle_known_states()
            if recovered:
                feedback = ((feedback or "") + " " + recovered).strip()
            shot = env.run_dir / "screenshots" / f"{step:02d}-observe.png"
            obs, png = await env.adapter.observe(seq=step, save_to=shot, input_values=env.input_values)
            env.log.emit(
                "observation",
                f"observed {obs.route} ({len(obs.controls)} controls, {len(obs.frames)} frames)",
                route=obs.route,
                frames=[f.route for f in obs.frames],
                controls=len(obs.controls),
                fingerprint=obs.fingerprint,
                screenshot=f"screenshots/{shot.name}",
                dialogs=obs.dialogs,
                blocked=obs.blocked,
            )
            if obs.fingerprint != prev_fp:
                no_progress = 0
            elif last_changes_ui:
                no_progress += 1
            prev_fp = obs.fingerprint
            if no_progress >= self.settings.limits.max_no_progress:
                return await self._stuck("stuck", "no state change for several steps", feedback)
            ctx = DiscoveryContext(
                goal=self.req.goal,
                inputs=self._declared(),
                route_handles=list(env.profile.route_handles),
                observation_text=render_observation(obs, list(self.extracted_values), env.redactor.for_model),
                screenshot_png=png,
                history=self.history,
                feedback=feedback,
                step=step,
                max_steps=self.req.max_steps,
                extracted=list(self.extracted_values),
                outputs=[(o.name, o.type.value) for o in self.req.expected_outputs],
            )
            res = await self.planner.decide(ctx)
            last_changes_ui = False
            self.usage["decisions"] += 1
            self.usage["input"] += res.input_tokens
            self.usage["output"] += res.output_tokens
            self.usage["latency"] += res.latency_ms
            if res.decision is None:
                invalid += 1
                env.log.emit(
                    "model_decision_invalid",
                    f"invalid model decision: {res.error}",
                    model=res.model,
                    input_tokens=res.input_tokens,
                    output_tokens=res.output_tokens,
                    latency_ms=res.latency_ms,
                )
                if res.error and res.error.startswith(("api_error", "connection_error", "rate_limited")) and invalid > 1:
                    raise Stop("internal_error", f"model provider unavailable: {res.error}")
                if invalid > self.settings.limits.max_invalid_decisions:
                    return await self._stuck("stuck", "no valid decision after bounded retries", feedback)
                feedback = f"Your last response was invalid ({res.error}). Call the act tool exactly once."
                continue
            invalid = 0
            d = res.decision
            last_changes_ui = d.action not in {"extract", "done"}
            env.log.emit(
                "model_decision",
                f"model chose {d.action}" + (f" on #{d.handle}" if d.handle else ""),
                step_id=f"d{step}",
                model=res.model,
                prompt_template_hash=self.planner.prompt_template_hash,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                cache_read_tokens=res.cache_read_tokens,
                latency_ms=res.latency_ms,
                decision=d.model_dump(exclude_none=True),
            )
            state_key = f"{obs.fingerprint}|{d.action}|{d.handle}|{d.input_name}|{d.literal_value}|{d.label_text}"
            if state_key in self.denied_keys:
                return await self._stuck("policy_violation", "model insisted on an action that policy denied", feedback)
            if state_key in seen and d.action not in {"done", "extract"}:
                return await self._stuck("stuck", f"repeated the same action on the same screen ({d.action})", feedback)
            seen.add(state_key)
            feedback = None
            if d.action == "give_up":
                return await self._stuck("stuck", f"model gave up: {d.rationale}", None)
            if d.action == "done":
                verified, detail = await self._verify_done(d, obs)
                if verified is not None:
                    return DiscoveryOutcome(verified, self.trajectory, outputs=dict(self.extracted_values))
                feedback = f"Verification of your done claim FAILED: {detail}. Fix it or continue."
                env.log.emit("verification", "done claim rejected", step_id=f"d{step}", detail=detail)
                continue
            if d.action == "extract":
                feedback = await self._extract(d, obs, step)
                continue
            if d.action == "wait":
                await env.adapter.wait_for(settle_condition(env), env.bindings, 2000)
                self.history.append("waited for the page")
                continue
            try:
                feedback = await self._act(d, obs, step, denied)
                if feedback and feedback.startswith("POLICY DENIED"):
                    self.denied_keys.add(state_key)
            except Stop as exc:
                if exc.escalate:
                    return await self._stuck(exc.code, str(exc), feedback)
                raise
        return await self._stuck("discovery_exhausted", "maximum action count reached", feedback)

    # ---------------------------------------------------------------- known states
    async def _handle_known_states(self) -> str | None:
        env = self.env
        notes = []
        for state in await scan_states(env):
            if state.id not in self.observed_states:
                self.observed_states.append(state.id)
            env.log.emit("state_detected", f"catalog state {state.id}", state_id=state.id, classification=state.classification)
            if state.classification == "hard_failure":
                evidence = await env.adapter.capture_evidence(state.id, env.run_dir, env.input_values)
                self.evidence.extend(evidence)
                raise Stop(state.error_code or state.id, f"{state.description}")
            if state.handler and state.handler.kind == "dismiss" and state.handler.target:
                resolved = await env.adapter.resolve(state.handler.target, env.bindings)
                action = await env.adapter.bind(BoundAction(kind=ActionKind.CLICK, target=state.handler.target), resolved)
                origin, route = await env.adapter.current_location()
                decision = env.policy.evaluate(action, mode="system", current_origin=origin, current_route=route)
                if decision.allowed:
                    await env.adapter.perform(action, resolved, token=env.token(), actor="automation")
                    env.log.emit("recovery_applied", f"{state.recovery_code}: {state.id}", state_id=state.id, code=state.recovery_code)
                    notes.append(f"The system dismissed a known '{state.id}' interstitial.")
            elif state.handler and state.handler.kind == "reauthenticate":
                if env.reauth_count >= 1:
                    raise Stop("session_recovery_exhausted", "session expired again after re-authentication")
                env.reauth_count += 1
                from interface_cua.runtime import login

                await login(env)
                await navigate(env, self.req.entry)
                env.log.emit("recovery_applied", "session_reauthenticated", state_id=state.id, code=state.recovery_code)
                notes.append("The session expired; the system signed in again and returned to the entry page.")
        return " ".join(notes) or None

    # ---------------------------------------------------------------- actions
    async def _act(self, d: DiscoveryDecision, obs: Observation, step: int, denied: dict[str, int]) -> str | None:
        env = self.env
        kind = KIND_MAP[d.action]
        resolved: Resolved | None = None
        target: TargetSpec | None = None
        value_ref: ValueRef | None = None
        value: str | None = None
        select_by = None
        control = obs.control(d.handle) if d.handle else None
        if kind != ActionKind.NAVIGATE:
            if control is None:
                return f"Control #{d.handle} does not exist on this screen. Use a number from the inventory."
            resolved = env.adapter.handle(d.handle or "")
            assert resolved is not None
            try:
                target = await env.adapter.build_target(resolved, self._describe(d, control.name, control.role), env.bindings)
            except ResolutionError as exc:
                return f"Control #{d.handle} could not be identified stably ({exc})."
        if kind in {ActionKind.TYPE, ActionKind.SELECT}:
            if d.input_name:
                if d.input_name not in env.input_values:
                    return f"'{d.input_name}' is not a declared input. Declared: {', '.join(env.input_values)}."
                value_ref, value = ValueRef(from_input=d.input_name), env.input_values[d.input_name]
                select_by = "value" if kind == ActionKind.SELECT else None
            elif d.literal_value is not None:
                ref = env.redactor.input_ref(d.literal_value)
                enum_ref = next(
                    (n for n, v in env.input_values.items() if env.bindings.vocab.get(v, "").lower() == d.literal_value.strip().lower()),
                    None,
                )
                if ref:  # the model echoed an input value: parameterize it, never keep it literal
                    value_ref, value = ValueRef(from_input=ref), d.literal_value
                    select_by = "value" if kind == ActionKind.SELECT else None
                elif enum_ref and kind == ActionKind.SELECT:  # the label of an enum input's value
                    value_ref, value, select_by = ValueRef(from_input=enum_ref), env.input_values[enum_ref], "value"
                else:
                    value_ref, value = ValueRef(literal=d.literal_value), d.literal_value
                    select_by = "label" if kind == ActionKind.SELECT else None
            else:
                return "type/select needs input_name (preferred) or literal_value."
            if kind == ActionKind.SELECT and select_by == "value" and control is not None:
                pass  # value-based selection of the declared enum input; checked by the adapter at perform time
        action = BoundAction(
            kind=kind,
            target=target,
            value_ref=value_ref,
            value=value,
            select_by=select_by,
            key=d.key,
            route_handle=d.route_handle,
            declared_intent_risk=RiskClass.IRREVERSIBLE if d.intent_risk == "irreversible" else None,
        )
        action = await env.adapter.bind(action, resolved)
        origin, route = await env.adapter.current_location()
        decision = env.policy.evaluate(action, mode="discovery", current_origin=origin, current_route=route)
        env.log.emit(
            "policy_decision",
            f"{kind.value} '{action.element_name or d.route_handle}': {decision.verdict}",
            step_id=f"d{step}",
            decision=decision,
            action=action.fingerprint(),
        )
        if not decision.allowed:
            fp = action.fingerprint()
            denied[fp] = denied.get(fp, 0) + 1
            env.log.emit(
                "action_blocked",
                f"blocked {kind.value} on '{action.element_name}'",
                step_id=f"d{step}",
                reasons=decision.reasons,
                risk=decision.risk,
                rule_ids=decision.rule_ids,
            )
            self.history.append(f"BLOCKED {kind.value} '{action.element_name}' ({decision.reasons[0]})")
            if denied[fp] > 1:
                raise Stop("policy_violation", f"model insisted on a denied action: {decision.reasons[0]}", escalate=True)
            return f"POLICY DENIED your action: {decision.reasons[0]}. Choose a different action that stays within policy."
        route_before = route
        t0 = time.monotonic()
        try:
            await env.adapter.perform(action, resolved, token=env.token(), actor="automation")
        except ActionError as exc:
            env.log.emit("action_failed", f"{kind.value} failed: {exc}", step_id=f"d{step}", code=exc.code)
            return f"The action failed ({exc.code}): {exc}. The page may be blocked by a modal or still loading."
        elapsed = int((time.monotonic() - t0) * 1000)
        await env.adapter.wait_for(settle_condition(env), env.bindings, 1500)
        _, route_after = await env.adapter.current_location()
        post_obs, _ = await env.adapter.observe(seq=step * 100, input_values=env.input_values)
        dialogs = [e.state_id for e in post_obs.dialogs if e.state_id]
        rec = RecordedStep(
            index=len(self.steps) + 1,
            provenance="model",
            action=kind,
            target=self._gen_target(target) if target else None,
            value=value_ref,
            select_by=select_by,
            key=d.key,
            route_handle=d.route_handle,
            risk=decision.risk,
            intent_risk=d.intent_risk,
            submits_form=action.submits_form,
            element_role=action.element_role,
            route_before=self._gen(route_before),
            route_after=self._gen(route_after),
            frames_after=self._frames_state(post_obs),
            dialog_states=dialogs,
            description=self._describe(d, action.element_name or "", action.element_role or ""),
            rationale=d.rationale[:200],
            elapsed_ms=elapsed,
            state_changed=post_obs.fingerprint != obs.fingerprint,
        )
        self.steps.append(rec)
        env.log.emit(
            "action_executed",
            rec.description,
            step_id=f"d{step}",
            recorded_index=rec.index,
            risk=decision.risk,
            elapsed_ms=elapsed,
            route_after=rec.route_after,
            locator_candidates=len(target.candidates) if target else 0,
            value_ref=value_ref,
        )
        self.history.append(rec.description + (f" -> now at {rec.route_after}" if rec.route_after != rec.route_before else ""))
        for e in post_obs.dialogs:
            self.history.append(f"(native dialog '{e.message[:60]}' {e.response}ed by the system)")
        return None

    def _describe(self, d: DiscoveryDecision, name: str, role: str) -> str:
        if d.action == "type":
            src = f"input {d.input_name}" if d.input_name else "literal text"
            return f"Type {src} into '{name}'"
        if d.action == "select":
            src = f"input {d.input_name}" if d.input_name else f"'{d.literal_value}'"
            return f"Select {src} in '{name}'"
        if d.action == "press":
            return f"Press {d.key} in '{name}'"
        if d.action == "navigate":
            return f"Navigate to {d.route_handle}"
        return f"Click {role} '{name}'"

    # ---------------------------------------------------------------- extract & verify
    def _value_cell_target(self, label: str, frame_index: int, obs: Observation) -> TargetSpec:
        frame = next((f for f in obs.frames if f.index == frame_index), obs.frames[0])
        fp = [FrameRef(name=p.name, route=None if p.name else p.route) for p in frame.path]
        return TargetSpec(
            description=f"value cell labelled '{label}'",
            frame_path=fp,
            candidates=[
                LabelAnchorLocator(
                    adapter_kinds=[self.env.adapter.kind],
                    anchor_text=label,
                    control="cell",
                    score=0.8,
                    rationale="Value read from the cell beside its visible label; no ids in legacy markup.",
                )
            ],
        )

    async def _extract(self, d: DiscoveryDecision, obs: Observation, step: int) -> str:
        env = self.env
        if not (d.output_name and d.output_type and d.label_text):
            return "extract needs output_name, output_type and label_text."
        declared = {o.name: o.type for o in self.req.expected_outputs}
        if declared and d.output_name not in declared:
            allowed = ", ".join(f"{n} ({t.value})" for n, t in declared.items())
            return f"'{d.output_name}' is not a required output. Use exactly one of: {allowed}."
        if declared and declared[d.output_name] != d.output_type:
            return f"Output {d.output_name} must have output_type {declared[d.output_name].value}."
        target = self._value_cell_target(d.label_text, d.frame_index or 0, obs)
        try:
            resolved = await env.adapter.resolve(target, env.bindings)
            raw = await env.adapter.read(resolved, "text")
            value = normalize(d.output_type, raw)
        except ResolutionError as exc:
            return f"No unique value next to label '{d.label_text}' in frame {d.frame_index or 0} ({exc.code})."
        except NormalizationError:
            return f"The value next to '{d.label_text}' is not a valid {d.output_type.value}."
        env.redactor.add_value(raw, d.output_type.value)
        rec = ExtractRecord(output_name=d.output_name, output_type=d.output_type, label_text=d.label_text, target=self._gen_target(target))
        self.extracts[d.output_name] = rec
        self.extracted_values[d.output_name] = value
        idx = len(self.steps)  # the extractor runs after the last recorded step
        self.extract_after[d.output_name] = idx
        env.log.emit(
            "extract",
            f"extracted {d.output_name} ({d.output_type.value}) from label '{d.label_text}'",
            step_id=f"d{step}",
            output=d.output_name,
            after_recorded_step=idx,
            value=env.redactor.output_value(value, "financial"),
        )
        self.history.append(f"Extracted {d.output_name} ({d.output_type.value}) from '{d.label_text}' (value verified by system)")
        return f"Extracted {d.output_name} successfully."

    async def _verify_done(self, d: DiscoveryDecision, obs: Observation) -> tuple[RunResult | None, str]:
        """Independent verification: checkpoint conditions evaluated deterministically on the live
        page, extractors re-run (the model's reading is never trusted), output-input consistency."""
        env = self.env
        checks = d.checkpoint or []
        if not checks:
            return None, "a done claim needs checkpoint checks (label/value pairs proving the final screen)"
        if env.input_values and not any(c.equals_input for c in checks):
            return None, "include at least one check that ties the final screen to a declared input (equals_input)"
        missing = [o.name for o in self.req.expected_outputs if o.name not in self.extracts]
        if missing:
            return None, f"required outputs not extracted yet: {missing}"
        verified_checks = []
        for c in checks:
            if c.equals_input and c.equals_input not in env.input_values:
                return None, f"checkpoint references unknown input {c.equals_input}"
            if not (c.equals_input or c.equals_text):
                return None, f"checkpoint for '{c.label_text}' needs equals_input or equals_text"
            target = self._value_cell_target(c.label_text, c.frame_index, obs)
            region = RegionSpec(frame_path=target.frame_path, target=target)
            refs = (
                [("equals_input", ValueRef(from_input=c.equals_input)), ("vocab_from_input", ValueRef(vocab_from_input=c.equals_input))]
                if c.equals_input
                else [("equals_text", ValueRef(literal=c.equals_text))]
            )
            matched_kind, detail = None, ""
            for kind, ref in refs:  # an enum input may be displayed by its tenant label, not its key
                check = await env.adapter.evaluate(TextInRegion(region=region, contains=ref), env.bindings)
                if check.holds:
                    matched_kind = kind
                    break
                detail = check.detail
            if matched_kind is None:
                return None, f"check '{c.label_text}' failed: {detail}"
            verified_checks.append(
                {
                    "label_text": c.label_text,
                    "frame_path": [f.model_dump(exclude_none=True) for f in self._gen_target(target).frame_path],
                    "equals_input": c.equals_input if matched_kind == "equals_input" else None,
                    "vocab_from_input": c.equals_input if matched_kind == "vocab_from_input" else None,
                    "equals_text": c.equals_text,
                }
            )
        for name, rec in self.extracts.items():  # re-extract deterministically now
            try:
                raw = await env.adapter.read(await env.adapter.resolve(rec.target, env.bindings), "text")
                value = normalize(rec.output_type, raw)
            except (ResolutionError, NormalizationError) as exc:
                return None, f"output {name} could not be re-read on the final screen ({exc})"
            if value != self.extracted_values[name]:
                return None, f"output {name} changed between extraction and completion"
        final_route = self._gen(obs.route)
        evidence = await env.adapter.capture_evidence("verified-final-state", env.run_dir, env.input_values)
        self.evidence.extend(evidence)
        env.log.emit("verification", "done claim verified deterministically", checks=len(verified_checks), outputs=list(self.extracts))
        traj = Trajectory(
            run_id=self.run_id,
            request=self.req,
            model=self.planner.model,
            prompt_template_hash=self.planner.prompt_template_hash,
            steps=self.steps,
            final_route=final_route,
            final_frames=self._frames_state(obs),
            checkpoint=verified_checks,
            outputs=[OutputDecl(name=n, type=r.output_type) for n, r in self.extracts.items()],
            extract=list(self.extracts.values()),
            observed_states=self.observed_states,
            routes_seen=sorted({self._gen(r) for r in env.adapter.routes_seen}),
            extract_after=self.extract_after,
        )
        self.trajectory = traj
        (env.run_dir / "trajectory.json").write_text(traj.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        desc = "; ".join(
            f"{c['label_text']} = "
            + (
                f"input {c['equals_input']}"
                if c["equals_input"]
                else f"label of input {c['vocab_from_input']}"
                if c["vocab_from_input"]
                else repr(c["equals_text"])
            )
            for c in verified_checks
        )
        result = SuccessResult(
            **self._base(),
            outputs=dict(self.extracted_values),
            checkpoint=desc,
            completed_by={f"d{s.index}": ("human" if s.provenance == "human" else "automation") for s in self.steps},
        )
        return result, "ok"

    # ---------------------------------------------------------------- stuck / escalation
    async def _stuck(self, code: str, message: str, feedback: str | None) -> DiscoveryOutcome:
        env = self.env
        from interface_cua.runtime import mine_unknown_state

        await mine_unknown_state(env, reason=f"discovery {code}: {message}", step_id=f"d{len(self.steps)}")
        evidence = await env.adapter.capture_evidence(f"stuck-{code}", env.run_dir, env.input_values)
        self.evidence.extend(evidence)
        if self.req.escalation_mode == "escalate" and self.escalator is not None:
            intervention = await self.escalator.escalate(
                env,
                kind="discovery_stuck",
                reason_code=code,
                explanation=message,
                goal=self.req.goal,
                step_id=f"d{len(self.steps)}",
                permitted=["resume", "abort"],
            )
            self.interventions.append(self.escalator.summary(intervention))
            res = intervention.resolution
            if res is not None and res.kind == "resume":
                for human in self.escalator.human_steps(intervention):
                    self.steps.append(
                        human.model_copy(
                            update={
                                "index": len(self.steps) + 1,
                                "route_before": self._gen(human.route_before),
                                "route_after": self._gen(human.route_after),
                                "target": self._gen_target(human.target) if human.target else None,
                            }
                        )
                    )
                    self.history.append("HUMAN: " + human.description)
                self.history.append(f"A human operator intervened: {res.note or 'no note'}")
                return await self._loop()
            code_ = "intervention_timeout" if intervention.status == "expired" else "operator_abort"
            return DiscoveryOutcome(
                AbortedResult(
                    **self._base(), code=code_, actor="system" if intervention.status == "expired" else "operator", reason=message
                ),
                None,
            )
        return DiscoveryOutcome(
            self._failure(code if code in {"timeout", "policy_violation"} else "discovery_exhausted", f"{code}: {message}"), None
        )


async def run_discovery(
    settings: Settings, request: DiscoveryRequest, planner: LLMClient, example_inputs: dict[str, str], escalator: Any = None
) -> DiscoveryOutcome:
    runner = DiscoveryRunner(settings, request, planner, escalator=escalator, example_inputs=example_inputs)
    return await runner.run()
