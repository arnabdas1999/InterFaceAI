"""Compile a verified discovery trajectory into a capability artifact, and apply authored patches.

Inputs are executed-action records and the reviewed app profile - never model prose. Concrete input
values never appear: bindings are ``from_input`` references and routes are patterns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.discovery.recorder import RecordedStep, Trajectory
from interface_cua.domain.actions import ActionKind, BoundAction, RiskClass
from interface_cua.domain.artifacts import (
    ArtifactPolicy,
    BusinessOutcomeDecl,
    CapabilityArtifact,
    CapabilityInfo,
    Compatibility,
    Contract,
    EntryPoint,
    Extractor,
    Lifecycle,
    LifecycleTransition,
    Provenance,
    RetryPolicy,
    ReviewInfo,
    StateMapping,
    Step,
    SuccessCondition,
    TargetApp,
    bump_version,
)
from interface_cua.domain.conditions import (
    AllOf,
    Condition,
    ElementPresent,
    FrameLoaded,
    TextInRegion,
    UrlMatches,
    ValueRef,
)
from interface_cua.domain.profiles import AppProfile, TenantProfile
from interface_cua.domain.routes import match_route
from interface_cua.domain.targets import (
    FrameRef,
    LabelAnchorLocator,
    LocatorCandidate,
    RegionSpec,
    RoleNameLocator,
    TargetSpec,
    TextLocator,
)
from interface_cua.domain.types import OutputType, json_schema_for
from interface_cua.policy.risk import classify

COMPILER_VERSION = "cua-compiler/1.0"
SENSITIVITY_BY_TYPE = {OutputType.MONEY: "financial", OutputType.DECIMAL: "financial"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Vocab:
    """Replace tenant-visible labels with vocabulary keys so one artifact serves many tenants."""

    def __init__(self, profile: AppProfile) -> None:
        self.by_value = {v: k for k, v in profile.vocabulary.items()}

    def key(self, text: str) -> str:
        stripped = text.strip().rstrip(":").strip()
        return f"vocab:{self.by_value[stripped]}" if stripped in self.by_value else text

    def target(self, t: TargetSpec) -> TargetSpec:
        cands: list[LocatorCandidate] = []
        for c in t.candidates:
            if isinstance(c, RoleNameLocator):
                cands.append(c.model_copy(update={"name": self.key(c.name)}))
            elif isinstance(c, LabelAnchorLocator):
                cands.append(c.model_copy(update={"anchor_text": self.key(c.anchor_text)}))
            elif isinstance(c, TextLocator):
                cands.append(c.model_copy(update={"text": self.key(c.text)}))
            else:
                cands.append(c)
        return t.model_copy(update={"candidates": cands})

    def value(self, text: str) -> ValueRef:
        k = self.key(text)
        return ValueRef(vocab=k[6:]) if k.startswith("vocab:") else ValueRef(literal=text)


def _frames_key(frames: list[Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(("/".join(f.name or f.route or "?" for f in fs.frame_path), fs.route) for fs in frames))


def _no_effect(step: RecordedStep, prev_frames: tuple[tuple[str, str], ...]) -> bool:
    return (
        step.action == ActionKind.CLICK
        and step.route_after == step.route_before
        and _frames_key(step.frames_after) == prev_frames
        and not step.dialog_states
        and step.element_role not in {"checkbox", "radio"}
        and step.provenance == "model"
        and not step.state_changed  # an in-place update (search results, SPA, desktop) is an effect
    )


def _param_bindings(route: str, inputs: set[str]) -> dict[str, ValueRef]:
    return {seg[1:]: ValueRef(from_input=seg[1:]) for seg in route.split("/") if seg.startswith(":") and seg[1:] in inputs}


def _postcondition(step: RecordedStep, prev_frames: tuple[tuple[str, str], ...], inputs: set[str]) -> Condition | None:
    conds: list[Condition] = []
    if step.route_after != step.route_before:
        conds.append(UrlMatches(route=step.route_after, params_equal=_param_bindings(step.route_after, inputs)))
    before = dict(prev_frames)
    for fs in step.frames_after:
        key = "/".join(f.name or f.route or "?" for f in fs.frame_path)
        if before.get(key) != fs.route and fs.route not in {"/blank", "/"}:
            conds.append(FrameLoaded(frame_path=fs.frame_path, route=fs.route))
    if not conds:
        return None
    return conds[0] if len(conds) == 1 else AllOf(conditions=conds)


def surface_of(tenant: TenantProfile) -> Literal["web", "legacy-web", "desktop"]:
    """Surface recorded in the artifact: desktop clients are desktop; SynthCore web deployments are legacy web."""
    return "desktop" if tenant.surface == "desktop" else "legacy-web"


def _in_place_postcondition(step: RecordedStep, kept: list[RecordedStep], traj: Trajectory, vocab: Vocab) -> Condition | None:
    """A click that updated the screen in place (no navigation): wait for what the flow needs next -
    the value about to be extracted (non-empty), else the next step's control."""
    extract_here = [e for e in traj.extract if traj.extract_after.get(e.output_name) == step.index]
    if extract_here:
        t = vocab.target(extract_here[0].target)
        return TextInRegion(region=RegionSpec(frame_path=t.frame_path, target=t), pattern=r"\S")
    later = [s for s in kept if s.index > step.index and s.target is not None]
    if later and later[0].target is not None:
        return ElementPresent(target=vocab.target(later[0].target))
    return None


def compile_trajectory(
    traj: Trajectory,
    profile: AppProfile,
    *,
    version: str = "1.0.0",
    app_profile_ref: str | None = None,
    surface_type: Literal["web", "legacy-web", "desktop"] = "legacy-web",
) -> CapabilityArtifact:
    req = traj.request
    vocab = Vocab(profile)
    input_names = {i.name for i in req.inputs}

    # 1-2. keep the executed, effective trajectory (drop no-effect exploratory clicks)
    kept: list[RecordedStep] = []
    dropped = 0
    prev_frames: tuple[tuple[str, str], ...] = ()
    frames_before: dict[int, tuple[tuple[str, str], ...]] = {}
    for s in traj.steps:
        if _no_effect(s, prev_frames):
            dropped += 1
            continue
        frames_before[s.index] = prev_frames
        kept.append(s)
        prev_frames = _frames_key(s.frames_after)
    if not kept:
        raise ValueError("trajectory has no effective steps")

    # 3-10. steps
    outcomes: dict[str, BusinessOutcomeDecl] = {}
    steps: list[Step] = []
    review_items: list[str] = []
    for n, s in enumerate(kept, start=1):
        sid = f"s{n}"
        target = vocab.target(s.target) if s.target else None
        states: list[StateMapping] = []
        # A step that submits: an HTML form submit, or a desktop command button (no forms on that surface).
        if s.submits_form or (surface_type == "desktop" and s.action == ActionKind.CLICK and s.element_role == "button"):
            for st in profile.state_catalog:
                if st.scope == "step" and any(match_route(p, s.route_before) is not None for p in st.applies_after_submit_on):
                    code = st.default_outcome_code or st.id
                    states.append(StateMapping(state=st.id, on="business_outcome", outcome_code=code))
                    outcomes.setdefault(
                        code,
                        BusinessOutcomeDecl(
                            code=code,
                            description=st.description,
                            details_schema={"type": "object", "properties": {"message": {"type": "string"}}, "additionalProperties": False},
                        ),
                    )
        idempotent = s.risk != RiskClass.IRREVERSIBLE and s.element_role not in {"checkbox", "radio"}
        post = _postcondition(s, frames_before[s.index], input_names)
        if post is None and s.action == ActionKind.CLICK and s.state_changed:
            post = _in_place_postcondition(s, kept, traj, vocab)
        pre: Condition | None = ElementPresent(target=target) if target else None
        value = s.value
        notes: list[str] = []
        if value is not None and value.literal is not None:
            value = vocab.value(value.literal)
            if value.literal is not None:
                notes.append(f"literal value '{value.literal}' kept; confirm it is not caller-specific")
        review = s.provenance == "human" or bool(target and target.review_required)
        if s.provenance == "human":
            notes.append("performed by a human operator during discovery")
        if target and all(c.strategy == "structural" for c in target.candidates):
            review = True
            notes.append("only a structural locator was available")
        if review or notes:
            review_items.append(f"{sid}: {s.description} - " + ("; ".join(notes) or "review locator"))
        steps.append(
            Step(
                id=sid,
                description=s.description,
                provenance=s.provenance,
                action=s.action,
                target=target,
                value=value,
                select_by=s.select_by,
                key=s.key,
                route_handle=s.route_handle,
                risk_class=s.risk,
                idempotent=idempotent,
                precondition=pre,
                postcondition=post,
                timeout_ms=max(10_000, min(60_000, s.elapsed_ms * 5)),
                retry=RetryPolicy(max_attempts=2, backoff_ms=400)
                if idempotent and s.action in {ActionKind.CLICK, ActionKind.NAVIGATE}
                else None,
                states=states,
                reentry_point=(n == 1),
                review_required=review,
                review_notes=notes,
                rationale=s.rationale,
                on_success=f"s{n + 1}" if n < len(kept) else "end",
            )
        )

    # extractors
    index_map = {s.index: f"s{n}" for n, s in enumerate(kept, start=1)}
    extractors: list[Extractor] = []
    for rec in traj.extract:
        after = traj.extract_after.get(rec.output_name, len(traj.steps))
        eligible = [i for i in index_map if i <= after]
        after_id = index_map[max(eligible)] if eligible else "s1"
        extractors.append(
            Extractor(
                output=rec.output_name,
                after_step=after_id,
                target=vocab.target(rec.target),
                method="text",
                type=rec.output_type,
                sensitivity=SENSITIVITY_BY_TYPE.get(rec.output_type, "pii"),
            )
        )

    kinds = ["desktop-uia"] if surface_type == "desktop" else ["web"]
    # success condition: final location + verified checkpoint checks (incl. output-input consistency)
    checks: list[Condition] = [UrlMatches(route=traj.final_route, params_equal=_param_bindings(traj.final_route, input_names))]
    for fs in traj.final_frames:
        if fs.route not in {"/blank", "/"}:
            checks.append(FrameLoaded(frame_path=fs.frame_path, route=fs.route))
    for c in traj.checkpoint:
        frame_path = [FrameRef.model_validate(f) for f in c["frame_path"]]
        cell = TargetSpec(
            description=f"value cell labelled '{c['label_text']}'",
            frame_path=frame_path,
            candidates=[
                LabelAnchorLocator(
                    adapter_kinds=kinds,
                    anchor_text=vocab.key(c["label_text"]),
                    control="cell",
                    score=0.8,
                    rationale="Checkpoint value read beside its label.",
                )
            ],
        )
        if c.get("equals_input"):
            ref = ValueRef(from_input=c["equals_input"])
        elif c.get("vocab_from_input"):
            ref = ValueRef(vocab_from_input=c["vocab_from_input"])
        else:
            ref = vocab.value(c["equals_text"])
        checks.append(TextInRegion(region=RegionSpec(frame_path=frame_path, target=cell), contains=ref))
    success_desc = "; ".join(
        f"{c['label_text']} shows "
        + (
            f"the input {c['equals_input']}"
            if c.get("equals_input")
            else f"the label of input {c['vocab_from_input']}"
            if c.get("vocab_from_input")
            else f"'{c['equals_text']}'"
        )
        for c in traj.checkpoint
    )

    # contract
    in_props: dict[str, Any] = {}
    for i in req.inputs:
        prop: dict[str, Any] = {
            "type": "integer" if i.type == "integer" else "string",
            "description": i.description,
            "x-sensitivity": i.sensitivity,
        }
        if i.pattern:
            prop["pattern"] = i.pattern
        if i.enum:
            prop["enum"] = i.enum
        in_props[i.name] = prop
    out_props: dict[str, Any] = {}
    for e in extractors:
        out_props[e.output] = {**json_schema_for(e.type, enum_values=e.enum_values), "x-sensitivity": e.sensitivity}
    side_effects = "irreversible" if any(s.risk_class == RiskClass.IRREVERSIBLE for s in steps) else "none"

    routes = set(traj.routes_seen) | {s.route_before for s in kept} | {s.route_after for s in kept}
    routes |= {fs.route for s in kept for fs in s.frames_after}
    if profile.login is not None:
        routes |= {profile.login.route, "/main"}
    entry_route = profile.route_handles[req.entry]
    routes.add(entry_route)
    actions = {s.action for s in steps} | {ActionKind.EXTRACT, ActionKind.WAIT_FOR, ActionKind.DIALOG_RESPOND, ActionKind.NAVIGATE}

    adapter_reqs = ["desktop-uia"] if surface_type == "desktop" else ["web"]
    if any(fs.frame_path for s in kept for fs in s.frames_after):
        adapter_reqs.append("web.frames")
    if any(s.dialog_states for s in kept):
        adapter_reqs.append("web.native_dialogs")

    artifact = CapabilityArtifact(
        capability=CapabilityInfo(
            id=req.capability_id, name=req.capability_name or req.capability_id, description=req.goal, version=version
        ),
        target=TargetApp(
            vendor_product=profile.vendor_product,
            app_profile=profile.id,
            app_profile_version_range=f">={profile.version},<{int(profile.version.split('.')[0]) + 1}.0.0",
            surface_type=surface_type,
            entry_point=EntryPoint(route_handle=req.entry, route=entry_route),
            requires_session="authenticated",
            adapter_requirements=adapter_reqs,
        ),
        contract=Contract(
            inputs={"type": "object", "properties": in_props, "required": sorted(in_props), "additionalProperties": False},
            outputs={"type": "object", "properties": out_props, "required": sorted(out_props), "additionalProperties": False},
            business_outcomes=list(outcomes.values()),
            side_effects=side_effects,
        ),
        policy=ArtifactPolicy(allowed_actions=sorted(actions), allowed_routes=sorted(routes)),
        steps=steps,
        extract=extractors,
        success=SuccessCondition(description=success_desc or "final screen verified", checks=checks),
        review=ReviewInfo(review_required=bool(review_items), items=review_items),
        provenance=Provenance(
            source_run_id=traj.run_id,
            discovery_goal=req.goal,
            model=traj.model,
            prompt_template_hash=traj.prompt_template_hash,
            compiler_version=COMPILER_VERSION,
            created_at=_now(),
            executed_actions=len(traj.steps),
            dropped_actions=dropped,
        ),
        compatibility=Compatibility(
            app_profile=app_profile_ref or f"{profile.id}@{profile.version}",
            product=profile.fingerprint.product,
            version_range=profile.fingerprint.version_range,
            base_tenant=req.tenant_id,
        ),
        lifecycle=Lifecycle(
            status="draft",
            transitions=[
                LifecycleTransition(
                    to="draft", at=_now(), actor=COMPILER_VERSION, note="compiled from verified discovery run", run_ids=[traj.run_id]
                )
            ],
        ),
    )
    return artifact.with_hash()


# ---------------------------------------------------------------------------- authored patches
class AuthoredPatch(BaseModel):
    """Reviewed, hand-written extension of an artifact (e.g. an irreversible commit step that the
    model is never allowed to learn by acting)."""

    model_config = ConfigDict(extra="forbid")

    patch_id: str
    description: str
    author: str
    append_steps: list[Step]
    extract: list[Extractor] = Field(default_factory=list)
    add_outputs: dict[str, Any] = Field(default_factory=dict)
    add_outcomes: list[BusinessOutcomeDecl] = Field(default_factory=list)
    side_effects: str | None = None
    success: SuccessCondition | None = None
    add_routes: list[str] = Field(default_factory=list)
    add_actions: list[ActionKind] = Field(default_factory=list)
    # facts for static risk re-classification of appended steps (policy re-checks live at run time)
    form_action_routes: dict[str, str] = Field(default_factory=dict)


def apply_patch(base: CapabilityArtifact, patch: AuthoredPatch, profile: AppProfile) -> CapabilityArtifact:
    steps = [s.model_copy() for s in base.steps]
    new_steps: list[Step] = []
    for s in patch.append_steps:
        role = name = None
        if s.target:
            rn = next((c for c in s.target.candidates if isinstance(c, RoleNameLocator)), None)
            if rn:
                role, name = rn.role, profile.vocabulary.get(rn.name[6:], rn.name) if rn.name.startswith("vocab:") else rn.name
        form_route = patch.form_action_routes.get(s.id)
        risk, _, _ = classify(
            BoundAction(
                kind=s.action,
                element_role=role,
                element_name=name,
                submits_form=form_route is not None,
                form_method="POST" if form_route else None,
                form_action_route=form_route,
            ),
            profile,
            current_route=_route_of(base),
        )
        risk = RiskClass.max(risk, s.risk_class)  # an authored step can never declare lower risk
        new_steps.append(
            s.model_copy(
                update={
                    "risk_class": risk,
                    "idempotent": s.idempotent and risk != RiskClass.IRREVERSIBLE,
                    "retry": None if risk == RiskClass.IRREVERSIBLE else s.retry,
                    "provenance": "authored",
                    "review_required": True,
                    "review_notes": [*s.review_notes, f"authored patch {patch.patch_id} by {patch.author}"],
                }
            )
        )
    steps[-1] = steps[-1].model_copy(update={"on_success": new_steps[0].id})
    contract = base.contract.model_copy(deep=True)
    outputs = dict(contract.outputs)
    props = {**outputs.get("properties", {}), **patch.add_outputs}
    outputs = {**outputs, "properties": props, "required": sorted(props)}
    side = patch.side_effects or (
        "irreversible" if any(s.risk_class == RiskClass.IRREVERSIBLE for s in new_steps) else contract.side_effects
    )
    contract = contract.model_copy(
        update={
            "outputs": outputs,
            "business_outcomes": [*contract.business_outcomes, *patch.add_outcomes],
            "side_effects": side,
        }
    )
    policy = ArtifactPolicy(
        allowed_actions=sorted({*base.policy.allowed_actions, *patch.add_actions, *(s.action for s in new_steps)}),
        allowed_routes=sorted({*base.policy.allowed_routes, *patch.add_routes}),
    )
    items = [*base.review.items, *(f"{s.id}: {s.description} - authored ({patch.patch_id}); risk {s.risk_class.value}" for s in new_steps)]
    draft = base.model_copy(
        update={
            "contract": contract,
            "policy": policy,
            "steps": [*steps, *new_steps],
            "extract": [*base.extract, *patch.extract],
            "success": patch.success or base.success,
            "review": ReviewInfo(review_required=True, items=items),
            "provenance": base.provenance.model_copy(
                update={
                    "parent": base.ref,
                    "authored_patches": [*base.provenance.authored_patches, patch.patch_id],
                    "compiler_version": COMPILER_VERSION,
                    "created_at": _now(),
                }
            ),
        }
    )
    version = bump_version(base, draft)
    result = CapabilityArtifact.model_validate(
        {
            **draft.model_dump(mode="json", exclude={"content_hash", "lifecycle"}),
            "capability": {**draft.capability.model_dump(), "version": version},
            "lifecycle": Lifecycle(
                status="draft",
                transitions=[
                    LifecycleTransition(to="draft", at=_now(), actor=COMPILER_VERSION, note=f"{base.ref} + authored patch {patch.patch_id}")
                ],
            ).model_dump(),
        }
    )
    return result.with_hash()


def _route_of(base: CapabilityArtifact) -> str:
    post = base.steps[-1].postcondition
    if isinstance(post, UrlMatches):
        return post.route
    if isinstance(post, AllOf):
        for c in post.conditions:
            if isinstance(c, UrlMatches):
                return c.route
    return base.success.checks[0].route if isinstance(base.success.checks[0], UrlMatches) else "/"
