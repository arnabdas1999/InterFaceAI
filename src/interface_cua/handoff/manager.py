"""Intervention manager: detect -> route -> pause -> human takes the SAME live session -> hand back.

Lives in the process that owns the browser. The operator API (same event loop) calls into it, so
no Playwright object ever crosses a process boundary. Control is transferred by the fenced lease:
pausing bumps the version (automation's token dies), a claim is compare-and-set, and every
human remote-control action goes through the same adapter with the human's token and policy.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from interface_cua.config import Settings
from interface_cua.domain.actions import ActionKind, BoundAction
from interface_cua.domain.artifacts import CapabilityArtifact
from interface_cua.domain.interventions import (
    ControlState,
    Intervention,
    InterventionKind,
    LeaseSnapshot,
    Resolution,
)
from interface_cua.domain.results import InterventionSummary
from interface_cua.domain.routes import canonicalize
from interface_cua.domain.targets import RoleNameLocator
from interface_cua.handoff.auth import OperatorDirectory
from interface_cua.handoff.control_lease import LeaseConflict, StaleLeaseError
from interface_cua.handoff.notifier import Notifier, default_notifier
from interface_cua.runtime import RunEnv
from interface_cua.storage.run_store import AuditStore


class HandoffError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class InterventionManager:
    def __init__(
        self,
        settings: Settings,
        *,
        notifier: Notifier | None = None,
        console_url: str | None = None,
        directory: OperatorDirectory | None = None,
    ) -> None:
        self.settings = settings
        self.directory = directory or OperatorDirectory(settings.config_dir / "operators.json", settings.state_dir / "operator_tokens.json")
        self._frame_queues: list[asyncio.Queue[bytes]] = []
        self.limits = settings.limits
        self.audit = AuditStore(settings.db_path)
        self.notifier = notifier or default_notifier(settings.state_dir / "outbox.jsonl")
        self.console_url = console_url or f"http://127.0.0.1:{settings.operator_port}"
        self.interventions: dict[str, Intervention] = {}
        self.env: RunEnv | None = None
        self._events: dict[str, asyncio.Event] = {}
        self._journal: dict[str, list[dict[str, Any]]] = {}
        self._remote_until = 0.0
        self._bg: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ live view (operator in control only)
    def subscribe_frames(self) -> asyncio.Queue[bytes]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3)
        self._frame_queues.append(queue)
        return queue

    def unsubscribe_frames(self, queue: asyncio.Queue[bytes]) -> None:
        if queue in self._frame_queues:
            self._frame_queues.remove(queue)

    def _on_frame(self, jpeg_b64: str) -> None:
        frame = jpeg_b64.encode("ascii")
        for queue in list(self._frame_queues):
            if queue.full():  # slow viewer: drop the oldest frame rather than buffer the session
                queue.get_nowait()
            queue.put_nowait(frame)

    async def start_live_view(self) -> None:
        if self.env is not None:
            await self.env.adapter.start_screencast(self._on_frame)

    async def stop_live_view(self) -> None:
        if self.env is not None:
            await self.env.adapter.stop_screencast()

    # ------------------------------------------------------------ binding to a run
    def attach(self, env: RunEnv) -> None:
        if self.env is env:
            return
        self.env = env
        original = env.lease._on_change

        def on_change(old: LeaseSnapshot, new: LeaseSnapshot, reason: str) -> None:
            if original:
                original(old, new, reason)
            self.audit.lease(env.run_id, old, new, reason)
            self._schedule_fence_sync()

        env.lease.set_listener(on_change)
        env.adapter.human_sink = self._on_headed_event

    # ------------------------------------------------------------ headed-window input fence
    async def sync_fence(self) -> None:
        """Direct input to the headed window is open only while a human holds a live lease, and only until
        its expiry (the page checks the expiry itself). Every other control state closes it."""
        env = self.env
        if env is None:
            return
        snap = env.lease.snapshot
        human = snap.state == ControlState.HUMAN_ACTIVE and snap.owner.startswith("human:")
        await env.adapter.set_human_input(snap.expires_at if human else None)

    def _schedule_fence_sync(self) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self.sync_fence())
        except RuntimeError:
            return  # no loop (synchronous unit use): nothing to fence
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def control(self) -> dict[str, Any]:
        env = self.env
        if env is None:
            return {"run": None}
        snap = env.lease.snapshot
        open_iv = next((i for i in self.interventions.values() if i.status in {"open", "claimed"}), None)
        return {"run_id": env.run_id, **snap.model_dump(mode="json"), "open_intervention": open_iv.id if open_iv else None}

    # ------------------------------------------------------------ escalation (called by the engine / runner)
    async def escalate(
        self,
        env: RunEnv,
        *,
        kind: InterventionKind,
        reason_code: str,
        explanation: str,
        permitted: list[str],
        capability: str | None = None,
        goal: str | None = None,
        step_id: str | None = None,
        step_description: str | None = None,
        bound_action: str | None = None,
        bound_action_hash: str | None = None,
        expected: str | None = None,
        observed: str | None = None,
        last_checkpoint_step: str | None = None,
        screenshot: str | None = None,
    ) -> Intervention:
        self.attach(env)
        lease = env.lease
        # The automation has finished its current action; stop issuing actions and cede control.
        lease.transition(
            ControlState.PAUSING, expect_version=lease.version, owner="automation", expected_owner="human", reason=f"escalation: {kind}"
        )
        lease.transition(
            ControlState.HUMAN_PENDING,
            expect_version=lease.version,
            owner="unassigned",
            expected_owner="human:any-operator",
            reason="awaiting operator claim",
        )
        now = time.time()
        _, route = await env.adapter.current_location()
        iv = Intervention(
            id=f"iv-{uuid.uuid4().hex[:8]}",
            run_id=env.run_id,
            kind=kind,
            capability=capability,
            goal=env.redactor.text(goal) if goal else None,
            tenant_id=env.tenant.tenant_id,
            step_id=step_id,
            step_description=step_description,
            bound_action=bound_action,
            bound_action_hash=bound_action_hash,
            last_checkpoint_step=last_checkpoint_step,
            reason_code=reason_code,
            explanation=env.redactor.text(explanation),
            route=env.redactor.text(route),
            screenshot=screenshot,
            expected=env.redactor.text(expected) if expected else None,
            observed=env.redactor.text(observed) if observed else None,
            control=lease.snapshot,
            permitted_resolutions=permitted,
            claim_deadline=now + self.limits.claim_sla_s,
            max_human_active_s=self.limits.human_active_max_s,
            approval_valid_s=self.limits.approval_valid_s,
            created_at=now,
        )
        self.interventions[iv.id] = iv
        self._events[iv.id] = asyncio.Event()
        self._journal[iv.id] = []
        before = env.adapter.session_handle()
        env.log.emit(
            "intervention_requested",
            f"{kind}: {iv.explanation}",
            step_id=step_id,
            intervention=iv.model_dump(mode="json"),
            session_id=before.session_id,
            context_id=before.context_id,
            route=iv.route,
        )
        env.log.write_json(f"intervention-{iv.id}.json", iv.model_dump(mode="json", exclude_none=True))
        # Routing may involve network I/O (webhook); keep it off the event loop that drives the live session.
        await asyncio.to_thread(self.notifier.notify, iv, self.console_url)
        iv.notified_at = time.time()
        self.audit.upsert(iv)
        await self._wait(iv)
        after = env.adapter.session_handle()
        env.log.emit(
            "intervention_resolved",
            f"{iv.id} {iv.status}: {iv.resolution.kind if iv.resolution else 'none'}",
            step_id=step_id,
            same_session=(before.session_id == after.session_id and before.context_id == after.context_id),
            session_id=after.session_id,
            context_id=after.context_id,
            route=after.route,
            human_actions=len(iv.human_actions),
            resolution=iv.resolution,
        )
        env.log.write_json(f"intervention-{iv.id}.json", iv.model_dump(mode="json", exclude_none=True))
        self.audit.upsert(iv)
        return iv

    async def _wait(self, iv: Intervention) -> None:
        env = self.env
        assert env is not None
        event = self._events[iv.id]
        while not event.is_set():
            try:
                await asyncio.wait_for(event.wait(), timeout=0.5)
            except TimeoutError:
                pass
            now = time.time()
            snap = env.lease.snapshot
            if iv.status == "open" and now > iv.claim_deadline:
                iv.status = "expired"
                env.lease.transition(ControlState.ABORTED, expect_version=snap.version, owner="system", reason="claim SLA expired")
                await self.stop_live_view()
                return
            if iv.status == "claimed" and snap.state == ControlState.HUMAN_ACTIVE:
                if iv.claimed_at and now - iv.claimed_at > iv.max_human_active_s:
                    iv.status = "expired"
                    env.lease.transition(
                        ControlState.ABORTED, expect_version=snap.version, owner="system", reason="human-active limit exceeded"
                    )
                    await self.stop_live_view()
                    return
                if snap.expires_at and now > snap.expires_at:  # heartbeats lost: return to the queue
                    await self.stop_live_view()
                    env.lease.transition(
                        ControlState.HUMAN_PENDING,
                        expect_version=snap.version,
                        owner="unassigned",
                        expected_owner="human:any-operator",
                        reason="operator heartbeat lost",
                    )
                    iv.status = "open"
                    iv.claim_deadline = now + self.limits.claim_sla_s

    # ------------------------------------------------------------ operator operations (called by the API)
    def get(self, iv_id: str) -> Intervention:
        iv = self.interventions.get(iv_id)
        if iv is None:
            raise HandoffError(404, f"no intervention {iv_id}")
        return iv

    def claim(self, iv_id: str, operator_id: str, expect_version: int | None = None) -> dict[str, Any]:
        iv = self.get(iv_id)
        env = self.env
        assert env is not None
        if iv.status != "open":
            raise HandoffError(409, f"intervention is {iv.status}")
        snap = env.lease.snapshot
        try:
            new = env.lease.transition(
                ControlState.HUMAN_ACTIVE,
                expect_version=expect_version if expect_version is not None else snap.version,
                owner=f"human:{operator_id}",
                expected_owner=f"human:{operator_id}",
                reason=f"claimed by {operator_id}",
                expires_in_s=self.limits.heartbeat_s * 3,
            )
        except LeaseConflict as exc:
            raise HandoffError(409, f"claim failed: {exc}") from exc
        iv.status = "claimed"
        iv.claimed_at = time.time()
        iv.operator_id = operator_id
        iv.control = new
        self.audit.upsert(iv)
        return {"token": new.version, "lease": new.model_dump(mode="json"), "heartbeat_s": self.limits.heartbeat_s}

    def heartbeat(self, iv_id: str, operator_id: str, token: int) -> dict[str, Any]:
        env = self.env
        assert env is not None
        self.get(iv_id)
        try:
            env.lease.heartbeat(token, f"human:{operator_id}", self.limits.heartbeat_s * 3)
        except StaleLeaseError as exc:
            raise HandoffError(409, str(exc)) from exc
        self._schedule_fence_sync()  # extend the headed window's fence to the new expiry
        return env.lease.snapshot.model_dump(mode="json")

    async def act(
        self,
        iv_id: str,
        operator_id: str,
        token: int,
        kind: str,
        *,
        x: float | None = None,
        y: float | None = None,
        text: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Remote control of the live page by the human in control (the mock co-browsing console)."""
        iv = self.get(iv_id)
        env = self.env
        assert env is not None
        actor = f"human:{operator_id}"
        try:
            env.lease.check(token, actor)
        except StaleLeaseError as exc:
            raise HandoffError(409, str(exc)) from exc
        entry: dict[str, Any] = {"at": time.time(), "operator": operator_id, "kind": kind, "source": "remote_console"}
        self._remote_until = time.time() + 5  # the in-page journal would echo this action; it is journaled here
        try:
            return await self._act(iv, env, entry, operator_id, token, actor, kind, x, y, text, key)
        finally:
            self._remote_until = time.time() + 0.4

    async def _act(
        self,
        iv: Intervention,
        env: RunEnv,
        entry: dict[str, Any],
        operator_id: str,
        token: int,
        actor: str,
        kind: str,
        x: float | None,
        y: float | None,
        text: str | None,
        key: str | None,
    ) -> dict[str, Any]:
        entry["_route_before"] = (await env.adapter.current_location())[1]  # raw, in-memory only
        if kind == "click":
            if x is None or y is None:
                raise HandoffError(400, "click needs x and y")
            resolved = await env.adapter.element_at(x, y)
            descr: dict[str, Any] = {}
            if resolved is not None:
                descr = await env.adapter.describe(resolved)
                action = await env.adapter.bind(BoundAction(kind=ActionKind.CLICK), resolved)
                origin, route = await env.adapter.current_location()
                decision = env.policy.evaluate(action, mode="human", current_origin=origin, current_route=route)
                entry["policy"] = decision.verdict
                entry["risk"] = decision.risk.value
                if not decision.allowed:
                    env.log.emit(
                        "action_blocked",
                        f"human click on '{descr.get('name')}' blocked: {decision.reasons[0]}",
                        mode="human",
                        step_id=iv.step_id,
                    )
                    raise HandoffError(403, f"blocked by policy: {decision.reasons[0]}")
                try:
                    entry["target"] = (
                        await env.adapter.build_target(resolved, f"{descr.get('role')} '{descr.get('name')}'", env.bindings)
                    ).model_dump(mode="json", exclude_none=True)
                except Exception:
                    pass
            entry.update({"role": descr.get("role"), "name": env.redactor.text(descr.get("name", "")), "x": round(x), "y": round(y)})
            await env.adapter.raw_click(x, y, token=token, actor=actor)
        elif kind == "type":
            if text is None:
                raise HandoffError(400, "type needs text")
            await self._gate_focused(iv, env, entry, BoundAction(kind=ActionKind.TYPE))  # the value never reaches policy or logs
            ref = env.redactor.input_ref(text)
            entry.update({"value_ref": ref, "length": len(text), "value_hash": env.redactor.hash8(text)})
            await env.adapter.raw_type(text, token=token, actor=actor)
        elif kind == "press":
            if key not in {"Enter", "Tab", "Escape"}:
                raise HandoffError(400, "key must be Enter, Tab or Escape")
            # Enter in a form submits it: bound to the focused control, it gets the same form-target checks as a click.
            await self._gate_focused(iv, env, entry, BoundAction(kind=ActionKind.PRESS, key=key))
            entry["key"] = key
            await env.adapter.raw_press(key, token=token, actor=actor)
        else:
            raise HandoffError(400, f"unknown action {kind}")
        _, route = await env.adapter.current_location()
        entry["route_after"] = env.redactor.text(route)
        entry["_route_after"] = route
        self._record(iv, entry)
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    async def _gate_focused(self, iv: Intervention, env: RunEnv, entry: dict[str, Any], action: BoundAction) -> None:
        """Policy check for keyboard input, evaluated against the control that would receive it."""
        resolved = await env.adapter.focused()
        if resolved is not None:
            descr = await env.adapter.describe(resolved)
            action = await env.adapter.bind(action, resolved)
            entry.update({"role": descr.get("role"), "name": env.redactor.text(descr.get("name", ""))})
        origin, route = await env.adapter.current_location()
        decision = env.policy.evaluate(action, mode="human", current_origin=origin, current_route=route)
        entry["policy"] = decision.verdict
        entry["risk"] = decision.risk.value
        if not decision.allowed:
            env.log.emit(
                "action_blocked",
                f"human {action.kind.value} on '{entry.get('name') or 'page'}' blocked: {decision.reasons[0]}",
                mode="human",
                step_id=iv.step_id,
            )
            raise HandoffError(403, f"blocked by policy: {decision.reasons[0]}")

    def _record(self, iv: Intervention, entry: dict[str, Any]) -> None:
        env = self.env
        assert env is not None
        # "target" and "_raw" keys stay in the in-memory journal (compiler input); never logged or returned.
        public = {k: v for k, v in entry.items() if k != "target" and not k.startswith("_")}
        iv.human_actions.append(public)
        self._journal[iv.id].append(entry)
        env.log.emit(
            "human_action",
            f"human {entry['kind']}" + (f" '{entry.get('name')}'" if entry.get("name") else ""),
            mode="human",
            step_id=iv.step_id,
            action=public,
        )

    async def _on_headed_event(self, payload: dict[str, Any]) -> None:
        """Direct manipulation of the headed window (journal script). Only called while a human owns the lease."""
        open_iv = next((i for i in self.interventions.values() if i.status == "claimed"), None)
        env = self.env
        if open_iv is None or env is None or time.time() < self._remote_until:
            return
        el = payload.get("el") or {}
        value = payload.get("value")
        entry: dict[str, Any] = {
            "at": time.time(),
            "operator": open_iv.operator_id,
            "kind": payload.get("kind"),
            "source": "headed_window",
            "role": el.get("role"),
            "name": env.redactor.text(el.get("name") or ""),
            "route_after": env.redactor.text(canonicalize(payload.get("url", "")).path),
            "_route_after": canonicalize(payload.get("url", "")).path,
            "_route_before": canonicalize(payload.get("url", "")).path,
        }
        target = _target_from_descriptor(el, payload.get("frame_name"))
        if target is not None:
            entry["target"] = target
        if value is not None:
            entry.update({"value_ref": env.redactor.input_ref(value), "length": len(value), "value_hash": env.redactor.hash8(value)})
        elif payload.get("value_len") is not None:
            entry["length"] = payload["value_len"]
        self._record(open_iv, entry)

    def resolve(self, iv_id: str, resolution: Resolution, token: int | None) -> Intervention:
        iv = self.get(iv_id)
        env = self.env
        assert env is not None
        if iv.status not in {"open", "claimed"}:
            raise HandoffError(409, f"intervention is {iv.status}")
        if resolution.kind not in iv.permitted_resolutions:
            raise HandoffError(400, f"{resolution.kind} not permitted; allowed: {iv.permitted_resolutions}")
        snap = env.lease.snapshot
        approval = resolution.kind in {"approve", "deny"}
        if approval:
            if resolution.kind == "approve" and resolution.action_hash != iv.bound_action_hash:
                raise HandoffError(409, "approval must echo the exact bound-action hash shown in the request")
        else:
            if iv.status != "claimed":
                raise HandoffError(409, "claim the session before resolving")
            try:
                env.lease.check(token if token is not None else -1, f"human:{resolution.operator_id}")
            except StaleLeaseError as exc:
                raise HandoffError(409, str(exc)) from exc
        if resolution.kind == "abort":
            env.lease.transition(
                ControlState.ABORTED,
                expect_version=snap.version,
                owner=f"human:{resolution.operator_id}",
                reason=f"aborted by {resolution.operator_id}",
            )
        else:
            env.lease.transition(
                ControlState.RESUMING,
                expect_version=snap.version,
                owner="automation",
                expected_owner="automation",
                reason=f"{resolution.kind} by {resolution.operator_id}",
            )
            env.lease.transition(
                ControlState.AUTOMATION_ACTIVE,
                expect_version=env.lease.version,
                owner="automation",
                reason="control handed back to automation",
            )
        iv.resolution = resolution.model_copy(update={"note": env.redactor.text(resolution.note)})
        iv.operator_id = iv.operator_id or resolution.operator_id
        iv.status = "resolved"
        iv.resolved_at = time.time()
        iv.control = env.lease.snapshot
        self.audit.upsert(iv)
        self._events[iv.id].set()
        return iv

    # ------------------------------------------------------------ engine helpers
    def summary(self, iv: Intervention) -> InterventionSummary:
        return InterventionSummary(
            intervention_id=iv.id,
            kind=iv.kind,
            step_id=iv.step_id,
            reason_code=iv.reason_code,
            resolution=iv.resolution.kind if iv.resolution else iv.status,
            operator_id=iv.operator_id,
            human_actions=len(iv.human_actions),
        )

    def human_performed_steps(self, iv: Intervention, artifact: CapabilityArtifact) -> set[str]:
        """Map journaled human clicks onto artifact steps (so an irreversible step done by a human is
        recognized as committed, never silently skipped)."""
        vocab = self.env.bindings.vocab if self.env else {}
        done: set[str] = set()
        for entry in self._journal.get(iv.id, []):
            for step in artifact.steps:
                if step.target is None:
                    continue
                for c in step.target.candidates:
                    if isinstance(c, RoleNameLocator):
                        name = vocab.get(c.name[6:], c.name) if c.name.startswith("vocab:") else c.name
                        if entry.get("role") == c.role and entry.get("name") == name:
                            done.add(step.id)
        return done

    def human_steps(self, iv: Intervention) -> list[Any]:
        """Journaled human clicks (remote console or headed window) as recorded discovery steps
        (provenance human, review required). Routes are raw here; the discovery runner generalizes them."""
        from interface_cua.discovery.recorder import RecordedStep
        from interface_cua.domain.actions import RiskClass
        from interface_cua.domain.targets import TargetSpec

        steps = []
        for entry in self._journal.get(iv.id, []):
            if entry.get("kind") != "click" or "target" not in entry:
                continue
            target = TargetSpec.model_validate(entry["target"]).model_copy(update={"review_required": True})
            steps.append(
                RecordedStep(
                    index=0,
                    provenance="human",
                    action=ActionKind.CLICK,
                    target=target,
                    risk=RiskClass(entry.get("risk", "reversible")),
                    intent_risk="human",
                    route_before=entry.get("_route_before", entry.get("_route_after", "/")),
                    route_after=entry.get("_route_after", "/"),
                    element_role=entry.get("role"),
                    description=f"Human clicked {entry.get('role')} '{entry.get('name')}'",
                )
            )
        return steps


def _target_from_descriptor(el: dict[str, Any], frame_name: str | None) -> dict[str, Any] | None:
    """Locator candidates for a click made directly in the headed window. There is no live element
    handle to verify against, so the target is always flagged for review."""
    from interface_cua.domain.targets import SCORES, FrameRef, StructuralLocator, TargetSpec

    cands: list[Any] = []
    if (
        el.get("name")
        and el.get("name_source") in {"aria", "label", "content", "value", "title"}
        and el.get("role") not in {"generic", None}
    ):
        cands.append(
            RoleNameLocator(
                role=el["role"],
                name=el["name"],
                score=SCORES["role_name"],
                rationale="Recorded from a human click in the headed window (unverified).",
            )
        )
    if el.get("css_path"):
        cands.append(StructuralLocator(css=el["css_path"], score=SCORES["structural"], rationale="Structural path of the clicked element."))
    if not cands:
        return None
    frames = [FrameRef(name=frame_name)] if frame_name else []
    spec = TargetSpec(description=f"{el.get('role')} '{el.get('name')}' (human)", frame_path=frames, candidates=cands, review_required=True)
    return spec.model_dump(mode="json", exclude_none=True)
