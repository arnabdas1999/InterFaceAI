"""Playwright implementation of the surface contract for (legacy) web apps.

Owns the browser context and live page/frame handles. Perception is accessibility-first with DOM
heuristics for unlabeled legacy controls (adjacent-cell labels), across all frames. Every action is
gated by the control-lease token; every request/navigation/pop-up is checked against policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Frame,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    async_playwright,
)
from playwright.async_api import Dialog as PWDialog
from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

from interface_cua.domain.actions import ActionKind, BoundAction
from interface_cua.domain.conditions import (
    AllOf,
    AnyOf,
    Condition,
    ConditionResult,
    DialogPresent,
    ElementAbsent,
    ElementPresent,
    FrameLoaded,
    HttpStatusIs,
    Not,
    TextInRegion,
    UrlMatches,
    ValueRef,
)
from interface_cua.domain.observations import Control, DialogEvent, FrameInfo, Observation, Rect
from interface_cua.domain.profiles import AppProfile
from interface_cua.domain.results import EvidenceRef, LocatorDiagnostic
from interface_cua.domain.routes import canonicalize, match_route
from interface_cua.domain.targets import (
    SCORES,
    AttributeLocator,
    CoordinateLocator,
    FrameRef,
    LabelAnchorLocator,
    LocatorCandidate,
    RoleNameLocator,
    StructuralLocator,
    TargetSpec,
    TextLocator,
)
from interface_cua.evidence.logger import RunLog, opaque_id
from interface_cua.handoff.control_lease import ControlLease
from interface_cua.policy.engine import PolicyEngine
from interface_cua.policy.redaction import Redactor
from interface_cua.surfaces.base import ActionError, Bindings, ResolutionError, Resolved, SessionHandle
from interface_cua.surfaces.browser import overlay, scripts

DialogDecider = Callable[[str, str], tuple[str, str, str | None]]
HumanEventSink = Callable[[dict[str, Any]], Awaitable[None] | None]

A11Y_SOURCES = {"aria", "label", "content", "value", "title"}


def resolve_value(ref: ValueRef, bindings: Bindings) -> str:
    if ref.from_input is not None:
        return bindings.inputs[ref.from_input]
    if ref.vocab is not None:
        return bindings.vocab.get(ref.vocab, ref.vocab)
    if ref.vocab_from_input is not None:
        value = bindings.inputs[ref.vocab_from_input]
        return bindings.vocab.get(value, value)
    return ref.literal or ""


def vocab_text(text: str, bindings: Bindings) -> str:
    return bindings.vocab.get(text[6:], text[6:]) if text.startswith("vocab:") else text


def _norm(s: str) -> str:
    return " ".join((s or "").split())


class BrowserAdapter:
    kind = "web"

    def __init__(
        self,
        *,
        profile: AppProfile,
        policy: PolicyEngine,
        redactor: Redactor,
        log: RunLog,
        lease: ControlLease,
        base_url: str,
        headed: bool = False,
        trace: bool = False,
        viewport: tuple[int, int] = (1280, 800),
    ) -> None:
        self.profile = profile
        self.policy = policy
        self.redactor = redactor
        self.log = log
        self.lease = lease
        self.base_url = base_url.rstrip("/")
        self.headed = headed
        self.trace_enabled = trace
        self.viewport = viewport
        self.session_id = opaque_id("sess")
        self.context_id = opaque_id("ctx")
        self.dialog_decider: DialogDecider | None = None
        self.human_sink: HumanEventSink | None = None
        self._human_until_ms = 0  # headed-window input fence: open for a human only until this lease expiry
        self._bg: set[asyncio.Task[None]] = set()
        self._last_blocked_log = 0.0
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._dialogs: list[DialogEvent] = []
        self._dialog_history: list[tuple[float, DialogEvent]] = []
        self._blocked: list[str] = []
        self.routes_seen: list[str] = []  # allowed document navigations (incl. redirects) for the compiler
        self.blocked_navigations: list[str] = []  # engine turns these into policy_violation
        self._frame_status: dict[str, int] = {}
        self._handles: dict[str, tuple[ElementHandle, Frame, int, dict[str, Any]]] = {}
        self._tracing = False
        self._cdp: Any = None
        self._sensitive_patterns = [[p.pattern, p.kind] for p in profile.sensitive_fields.patterns]
        self._sensitive_labels = [[lbl.label_pattern, lbl.kind] for lbl in profile.sensitive_fields.labels]

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=not self.headed)
        self._context = await self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]}, accept_downloads=False
        )
        await self._context.route("**/*", self._route_handler)
        await self._context.expose_binding("cuaHumanEvent", self._on_human_event)
        await self._context.add_init_script(scripts.INPUT_FENCE_INIT)  # before the journal: blocked input is never journaled
        await self._context.add_init_script(scripts.HUMAN_JOURNAL_INIT)
        if self.headed:  # only a person at the window needs to see whether their input will land
            await self._context.add_init_script(scripts.FENCE_BADGE_INIT)
        self.page = await self._context.new_page()
        self.page.on("framenavigated", self._on_frame_navigated)
        self._context.on("page", self._on_new_page)
        self.page.on("dialog", self._on_dialog)
        self.page.on("response", self._on_response)
        self.log.session_id = self.session_id
        self.log.emit(
            "session_started",
            "browser session opened",
            session_id=self.session_id,
            context_id=self.context_id,
            headed=self.headed,
        )

    async def start_trace(self) -> None:
        """Started after login so the trace never contains the credential fill."""
        if self.trace_enabled and self._context and not self._tracing:
            await self._context.tracing.start(screenshots=True, snapshots=True)
            self._tracing = True

    async def close(self, trace_path: Path | None = None) -> None:
        try:
            if self._tracing and self._context:
                await self._context.tracing.stop(path=str(trace_path) if trace_path else None)
        except PWError:
            pass
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except PWError:
                pass
        if self._pw:
            await self._pw.stop()

    def session_handle(self) -> SessionHandle:
        route = canonicalize(self.page.url).path if self.page else ""
        return SessionHandle(session_id=self.session_id, context_id=self.context_id, route=route)

    # ------------------------------------------------------------------ guards
    async def _route_handler(self, route: Route, request: Request) -> None:
        ok, reason = self.policy.url_allowed(request.url)
        if ok:
            if request.is_navigation_request():
                self.routes_seen.append(canonicalize(request.url).path)
                if request.method == "GET":
                    # Redirect hops are not visible to route interception, so fetch without following
                    # redirects and check the Location against policy before the browser follows it.
                    try:
                        response = await route.fetch(max_redirects=0)
                    except PWError:
                        await route.abort("failed")
                        return
                    if 300 <= response.status < 400:
                        location = urljoin(request.url, response.headers.get("location", ""))
                        ok_loc, why = self.policy.url_allowed(location)
                        if not ok_loc:
                            cu = canonicalize(location)
                            self._blocked.append(f"{cu.origin}{cu.path}")
                            if self._is_own_page(request):
                                self.blocked_navigations.append(f"{cu.origin}{cu.path}")
                            self.log.emit(
                                "request_blocked",
                                "blocked redirect outside allowlist",
                                url=f"{cu.origin}{cu.path}",
                                reason=why,
                                navigation=True,
                                redirect=True,
                            )
                            await route.abort("blockedbyclient")
                            return
                    await route.fulfill(response=response)
                    return
            await route.continue_()
            return
        cu = canonicalize(request.url)
        self._blocked.append(f"{cu.origin}{cu.path}")
        if request.is_navigation_request() and self._is_own_page(request):
            self.blocked_navigations.append(f"{cu.origin}{cu.path}")
        self.log.emit(
            "request_blocked",
            f"blocked {request.resource_type} request outside allowlist",
            url=f"{cu.origin}{cu.path}",
            reason=reason,
            navigation=request.is_navigation_request(),
        )
        await route.abort("blockedbyclient")

    def _is_own_page(self, request: Request) -> bool:
        """Navigation of the automation's page or its frames (not a pop-up, which is closed separately)."""
        try:
            return request.frame.page is self.page
        except PWError:
            return False

    async def _on_new_page(self, page: Page) -> None:
        if page is self.page:
            return
        url = page.url
        try:
            await page.close()
        except PWError:
            pass
        cu = canonicalize(url) if url else None
        self._blocked.append(f"popup:{cu.origin + cu.path if cu else 'about:blank'}")
        self.log.emit("popup_blocked", "closed an unexpected pop-up window", url=str(cu) if cu else None)

    def _on_response(self, response: Response) -> None:
        req = response.request
        if req.is_navigation_request():
            self._frame_status[canonicalize(response.url).path] = response.status
            try:
                self._frame_status[f"frame:{id(req.frame)}"] = response.status
            except PWError:
                pass

    async def _on_dialog(self, dialog: PWDialog) -> None:
        dtype, message = dialog.type, dialog.message
        response, reason, state_id = ("dismiss", "no dialog policy installed", None)
        if self.dialog_decider is not None:
            response, reason, state_id = self.dialog_decider(dtype, message)
        try:
            if response == "accept":
                await dialog.accept()
            else:
                await dialog.dismiss()
        except PWError:
            pass
        event = DialogEvent(
            dialog_type=dtype,
            message=self.redactor.for_model(message)[:300],
            response=response,
            reason=reason,
            state_id=state_id,
        )
        self._dialogs.append(event)
        self._dialog_history.append((time.monotonic(), event))
        self.log.emit("dialog", f"native {dtype} dialog {response}ed", dialog=event, state_id=state_id)

    async def _on_human_event(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        if payload.get("kind") == "blocked_input":
            if time.monotonic() - self._last_blocked_log > 2:
                self._last_blocked_log = time.monotonic()
                self.log.emit(
                    "human_input_blocked",
                    f"direct input to the headed window blocked ({payload.get('input')}): no live human lease",
                    control_state=self.lease.snapshot.state.value,
                )
            return
        if not self.lease.owner.startswith("human:"):
            return  # automation-generated input; attribution comes from the lease, not isTrusted
        if self.human_sink is not None:
            res = self.human_sink({"source": "headed_window", **payload})
            if asyncio.iscoroutine(res):
                await res

    def drain_dialogs(self) -> list[DialogEvent]:
        out, self._dialogs = self._dialogs, []
        return out

    def drain_blocked(self) -> list[str]:
        out, self._blocked = self._blocked, []
        return out

    def forget_dialogs(self) -> None:
        self._dialog_history.clear()

    # ------------------------------------------------------------------ frames
    def _frames(self) -> list[tuple[int, Frame, list[FrameRef]]]:
        assert self.page is not None
        out: list[tuple[int, Frame, list[FrameRef]]] = []

        def walk(frame: Frame, path: list[FrameRef]) -> None:
            out.append((len(out), frame, path))
            for child in frame.child_frames:
                walk(child, [*path, FrameRef(name=child.name or None, route=canonicalize(child.url).path)])

        walk(self.page.main_frame, [])
        return out

    def _resolve_frame(self, path: list[FrameRef]) -> tuple[Frame, int] | None:
        frames = self._frames()
        assert self.page is not None
        frame = self.page.main_frame
        for ref in path:
            nxt = None
            for child in frame.child_frames:
                if ref.name and child.name == ref.name:
                    nxt = child
                    break
            if nxt is None and ref.route:
                for child in frame.child_frames:
                    if match_route(ref.route, canonicalize(child.url).path) is not None:
                        nxt = child
                        break
            if nxt is None:
                return None
            frame = nxt
        idx = next((i for i, f, _ in frames if f is frame), 0)
        return frame, idx

    async def current_location(self) -> tuple[str, str]:
        assert self.page is not None
        cu = canonicalize(self.page.url)
        return cu.origin, cu.path

    async def generator(self) -> str | None:
        assert self.page is not None
        try:
            result = await self.page.main_frame.evaluate(scripts.GENERATOR)
            return str(result) if result else None
        except PWError:
            return None

    # ------------------------------------------------------------------ perception
    async def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        if frame.parent_frame is None:
            return 0.0, 0.0
        try:
            fe = await frame.frame_element()
            box = await fe.bounding_box()
            border = await fe.evaluate("e => [e.clientLeft || 0, e.clientTop || 0]")
            if box:
                return box["x"] + float(border[0]), box["y"] + float(border[1])
        except PWError:
            pass
        return 0.0, 0.0

    async def _scan_sensitive(self, frame: Frame, input_values: dict[str, str]) -> dict[str, Any]:
        patterns = [*self._sensitive_patterns]
        for secret in self.redactor.secret_values():  # e.g. the service-account name shown in the header
            patterns.append([re.escape(secret), "secret"])
        for name, value in input_values.items():
            if value and len(value) >= 3:
                patterns.append([rf"(?<![\w]){re.escape(value)}(?![\w])", f"input:{name}"])
        result: dict[str, Any] = await frame.evaluate(scripts.SENSITIVE_SCAN, {"labels": self._sensitive_labels, "patterns": patterns})
        return result

    def _mask_frames(self) -> list[tuple[int, Frame, list[FrameRef]]]:
        """Every frame of the page is masked, including shell frames outside the application frame."""
        return self._frames()

    async def _masks(self, input_values: dict[str, str]) -> list[tuple[Rect, str]]:
        masks: list[tuple[Rect, str]] = []
        for _, frame, _ in self._mask_frames():
            try:
                scan = await self._scan_sensitive(frame, input_values)
            except PWError:
                continue
            ox, oy = await self._frame_offset(frame)
            for cell in scan["cells"]:
                if self.redactor.input_ref(cell["text"]) is None:
                    self.redactor.add_value(cell["text"], cell["kind"])
                masks.append((_rect(cell["rect"], ox, oy), cell["kind"]))
            for inp in scan["inputs"]:
                ref = self.redactor.input_ref(inp["value"])
                if inp["type"] == "password":
                    masks.append((_rect(inp["rect"], ox, oy), "secret"))
                elif ref is not None:
                    masks.append((_rect(inp["rect"], ox, oy), f"input:{ref}"))
            for m in scan["matches"]:
                masks.append((_rect(m["rect"], ox, oy), m["kind"]))
        return masks

    async def observe(
        self, *, seq: int, save_to: Path | None = None, input_values: dict[str, str] | None = None
    ) -> tuple[Observation, bytes]:
        """Normalized observation + the masked, set-of-marks screenshot sent to the model."""
        assert self.page is not None
        input_values = input_values or {}
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PWError:
            pass
        masks = await self._masks(input_values)
        controls: list[Control] = []
        handles: dict[str, tuple[ElementHandle, Frame, int, dict[str, Any]]] = {}
        frames_info: list[FrameInfo] = []
        texts: dict[int, str] = {}
        n = 0
        for idx, frame, path in self._frames():
            cu = canonicalize(frame.url)
            frames_info.append(
                FrameInfo(
                    index=idx,
                    path=path,
                    name=frame.name or None,
                    route=cu.path,
                    origin=cu.origin,
                    status=self._frame_status.get(f"frame:{id(frame)}"),
                )
            )
            try:
                inv = await frame.evaluate_handle(scripts.INVENTORY)
                items: list[dict[str, Any]] = await inv.evaluate("r => r.items")
                els_handle = await inv.evaluate_handle("r => r.els")
                props = await els_handle.get_properties()
                text = await frame.evaluate(scripts.PAGE_TEXT)
            except PWError:
                continue
            texts[idx] = self.redactor.for_model(str(text))[:4000]
            for i, item in enumerate(items):
                el = props[str(i)].as_element()
                if el is None:
                    continue
                n += 1
                handle = str(n)
                box = await el.bounding_box()
                controls.append(self._to_control(handle, item, idx, path, box))
                handles[handle] = (el, frame, idx, item)
        self._handles = handles
        origin, route = await self.current_location()
        title = await self.page.title()
        fp_src = route + "|" + "|".join(f"{c.role}:{c.name}:{c.frame_index}:{c.value}:{c.enabled}" for c in controls)
        fp_src += "|" + "|".join(f"{i}:{t[:500]}" for i, t in texts.items())
        obs = Observation(
            seq=seq,
            route=route,
            origin=origin,
            title=self.redactor.for_model(title),
            frames=frames_info,
            controls=controls,
            text=texts,
            dialogs=self.drain_dialogs(),
            blocked=self.drain_blocked(),
            generator=await self.generator(),
            fingerprint=hashlib.sha256(fp_src.encode()).hexdigest()[:16],
        )
        raw = await self.page.screenshot()
        model_png = overlay.render(raw, masks, controls)
        if save_to is not None:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(model_png)
            obs = obs.model_copy(update={"screenshot_path": save_to.name})
        return obs, model_png

    def _to_control(self, handle: str, it: dict[str, Any], idx: int, path: list[FrameRef], box: Any) -> Control:
        value = it.get("value")
        if value:
            value = "⟦secret⟧" if it.get("input_type") == "password" else self.redactor.for_model(value)
        href_route = href_origin = None
        if it.get("href"):
            cu = canonicalize(it["href"])
            href_route, href_origin = cu.path, cu.origin
        form_route = canonicalize(it["form_action"]).path if it.get("form_action") else None
        return Control(
            handle=handle,
            role=it["role"],
            name=self.redactor.for_model(it["name"])[:120],
            name_source=it["name_source"],
            tag=it["tag"],
            input_type=it.get("input_type"),
            value=value,
            frame_index=idx,
            frame_path=path,
            bbox=Rect(x=box["x"], y=box["y"], w=box["width"], h=box["height"]) if box else None,
            enabled=bool(it.get("enabled", True)) and not it.get("obscured", False),
            checked=it.get("checked"),
            options=[self.redactor.for_model(o) for o in it.get("options", [])],
            href_route=href_route,
            href_origin=href_origin,
            form_method=it.get("form_method"),
            form_action_route=form_route,
            submits_form=bool(it.get("submits_form")),
            anchor_text=it.get("anchor_text"),
            attributes=it.get("attributes", {}),
            css_path=it.get("css_path"),
        )

    def handle(self, handle: str) -> Resolved | None:
        entry = self._handles.get(handle)
        if entry is None:
            return None
        el, frame, idx, _ = entry
        return Resolved(element=el, frame=frame, frame_index=idx, candidate_index=0, strategy="handle")

    # ------------------------------------------------------------------ targeting
    async def _visible(self, els: list[ElementHandle]) -> list[ElementHandle]:
        out = []
        for e in els:
            try:
                if await e.is_visible():
                    out.append(e)
            except PWError:
                continue
        return out

    async def _candidate_elements(self, frame: Frame, cand: LocatorCandidate, b: Bindings) -> list[ElementHandle]:
        try:
            if isinstance(cand, RoleNameLocator):
                loc = frame.get_by_role(cand.role, name=vocab_text(cand.name, b), exact=cand.exact)  # type: ignore[arg-type]
                return await self._visible(await loc.element_handles())
            if isinstance(cand, LabelAnchorLocator):
                arr = await frame.evaluate_handle(
                    scripts.LABEL_ANCHOR,
                    {"anchor": vocab_text(cand.anchor_text, b), "control": cand.control, "inputType": cand.input_type},
                )
                props = await arr.get_properties()
                return [p.as_element() for _, p in sorted(props.items(), key=lambda kv: int(kv[0])) if p.as_element()]  # type: ignore[misc]
            if isinstance(cand, AttributeLocator):
                css = cand.tag + "".join(f'[{k}="{v}"]' for k, v in cand.attributes.items())
                return await self._visible(await frame.locator(css).element_handles())
            if isinstance(cand, TextLocator):
                arr = await frame.evaluate_handle(scripts.TEXT_TARGETS, {"text": vocab_text(cand.text, b), "tag": cand.tag})
                props = await arr.get_properties()
                return [p.as_element() for _, p in sorted(props.items(), key=lambda kv: int(kv[0])) if p.as_element()]  # type: ignore[misc]
            if isinstance(cand, StructuralLocator):
                return await self._visible(await frame.locator(cand.css).element_handles())
            if isinstance(cand, CoordinateLocator):
                h = await frame.evaluate_handle(scripts.ELEMENT_AT_POINT, [cand.x, cand.y])
                el = h.as_element()
                return [el] if el else []
        except PWError:
            return []
        return []

    async def resolve(self, target: TargetSpec, bindings: Bindings) -> Resolved:
        """Resolve candidates in priority order and require exactly one match.

        Zero matches -> try the next candidate. More than one match -> stop (ambiguous): a weaker
        fallback must not silently pick among duplicates of the strongest identity signal.
        """
        found = self._resolve_frame(target.frame_path)
        if found is None:
            raise ResolutionError(
                "locator_not_found",
                f"frame path {[f.name or f.route for f in target.frame_path]} not present",
                [LocatorDiagnostic(strategy="frame_path", match_count=0, detail="frame not found")],
            )
        frame, idx = found
        diags: list[LocatorDiagnostic] = []
        for rank, cand in enumerate(target.ordered()):
            if self.kind not in cand.adapter_kinds:
                diags.append(LocatorDiagnostic(strategy=cand.strategy, match_count=-1, detail="not interpretable by web adapter"))
                continue
            els = await self._candidate_elements(frame, cand, bindings)
            diags.append(LocatorDiagnostic(strategy=cand.strategy, match_count=len(els), detail=_cand_detail(cand)))
            if len(els) == 1 and cand.expected_count == 1:
                return Resolved(
                    element=els[0], frame=frame, frame_index=idx, candidate_index=rank, strategy=cand.strategy, diagnostics=diags
                )
            if len(els) > 1:
                raise ResolutionError("locator_ambiguous", f"{cand.strategy} matched {len(els)} controls", diags)
        raise ResolutionError("locator_not_found", f"no candidate matched: {target.description}", diags)

    async def count(self, target: TargetSpec, bindings: Bindings) -> int:
        found = self._resolve_frame(target.frame_path)
        if found is None:
            return 0
        for cand in target.ordered():
            if self.kind in cand.adapter_kinds:
                n = len(await self._candidate_elements(found[0], cand, bindings))
                if n:
                    return n
        return 0

    async def describe(self, resolved: Resolved) -> dict[str, Any]:
        result: dict[str, Any] = await resolved.element.evaluate(scripts.DESCRIBE)
        return result

    async def build_target(self, resolved: Resolved, description: str, bindings: Bindings) -> TargetSpec:
        """Record a locator bundle from the live element *at action time*, keeping only candidates
        that resolve uniquely to this same element right now (candidate agreement)."""
        d = await self.describe(resolved)
        frames = self._frames()
        path = next((p for i, _, p in frames if i == resolved.frame_index), [])
        frame_path = [FrameRef(name=f.name, route=None if f.name else f.route) for f in path]
        cands: list[LocatorCandidate] = []
        role, name, src = d["role"], d["name"], d["name_source"]
        if name and src in A11Y_SOURCES and role not in {"generic", "cell"}:
            cands.append(
                RoleNameLocator(
                    role=role,
                    name=name,
                    score=SCORES["role_name"],
                    rationale=f"Accessible role '{role}' with name from {src}; survives layout and markup changes.",
                )
            )
        if d.get("anchor_text"):
            control = {
                "textbox": "input",
                "combobox": "select",
                "button": "button",
                "link": "link",
                "checkbox": "checkbox",
                "cell": "cell",
            }.get(role)
            if control:
                cands.append(
                    LabelAnchorLocator(
                        anchor_text=d["anchor_text"],
                        control=control,
                        input_type="password" if d.get("input_type") == "password" else None,
                        score=SCORES["role_name_inferred"] if src == "adjacent_cell" else SCORES["label_anchor"],
                        rationale=f"Control in the table row labelled '{d['anchor_text']}'; legacy markup has no <label> wiring.",
                    )
                )
        attrs = {k: v for k, v in d.get("attributes", {}).items() if k in {"name", "id"}}
        if attrs:
            cands.append(
                AttributeLocator(
                    tag=d["tag"],
                    attributes=attrs,
                    score=SCORES["attribute"],
                    rationale="Stable, non-generated attribute(s) on the control.",
                )
            )
        if role in {"link", "cell"} and d.get("text") and not any(isinstance(c, RoleNameLocator) for c in cands):
            cands.append(TextLocator(text=d["text"], tag=d["tag"], score=SCORES["text"], rationale="Exact visible text."))
        if d.get("css_path"):
            cands.append(
                StructuralLocator(
                    css=d["css_path"],
                    score=SCORES["structural"],
                    rationale="Structural path; brittle, kept as a late fallback only.",
                )
            )
        verified: list[LocatorCandidate] = []
        for cand in cands:
            els = await self._candidate_elements(resolved.frame, cand, bindings)
            if len(els) == 1 and await els[0].evaluate("(a, b) => a === b", resolved.element):
                verified.append(cand)
        review = False
        if not verified:
            box = await resolved.element.bounding_box()
            if box is None:
                raise ResolutionError("locator_not_found", "element has no stable identity and no box", [])
            verified.append(
                CoordinateLocator(
                    x=box["x"] + box["width"] / 2,
                    y=box["y"] + box["height"] / 2,
                    viewport=self.viewport,
                    score=SCORES["coordinate"],
                    rationale="No unique semantic locator; coordinate fallback requires review.",
                )
            )
            review = True
        return TargetSpec(description=description, frame_path=frame_path, candidates=verified, review_required=review)

    async def bind(self, action: BoundAction, resolved: Resolved | None) -> BoundAction:
        """Attach facts about the live element that target-level risk rules need."""
        if resolved is None:
            return action
        d = await self.describe(resolved)
        href_route = href_origin = None
        if d.get("href"):
            cu = canonicalize(d["href"])
            href_route, href_origin = cu.path, cu.origin
        submits = bool(d.get("submits_form")) or (
            action.kind == ActionKind.PRESS and action.key == "Enter" and d.get("form_method") is not None
        )
        return action.model_copy(
            update={
                "element_role": d["role"],
                "element_name": d["name"],
                "submits_form": submits,
                "form_method": d.get("form_method"),
                "form_action_route": canonicalize(d["form_action"]).path if d.get("form_action") else None,
                "href_route": href_route,
                "href_origin": href_origin,
            }
        )

    # ------------------------------------------------------------------ acting
    async def perform(self, action: BoundAction, resolved: Resolved | None, *, token: int, actor: str, timeout_ms: int = 10_000) -> None:
        self.lease.check(token, actor)
        assert self.page is not None
        el: ElementHandle | None = resolved.element if resolved else None
        try:
            match action.kind:
                case ActionKind.NAVIGATE:
                    route = self.profile.route_handles[action.route_handle or ""]
                    await self.page.goto(self.base_url + route, wait_until="domcontentloaded", timeout=timeout_ms)
                case ActionKind.CLICK:
                    assert el is not None
                    async with self._automation_input():
                        await el.click(timeout=timeout_ms)
                case ActionKind.TYPE:
                    assert el is not None
                    async with self._automation_input():
                        await el.fill(action.value or "", timeout=timeout_ms)
                case ActionKind.SELECT:
                    assert el is not None
                    async with self._automation_input():
                        if action.select_by == "label":
                            await el.select_option(label=action.value or "", timeout=timeout_ms)
                        else:
                            await el.select_option(value=action.value or "", timeout=timeout_ms)
                case ActionKind.PRESS:
                    async with self._automation_input():
                        if el is not None:
                            await el.press(action.key or "Enter", timeout=timeout_ms)
                        else:
                            await self.page.keyboard.press(action.key or "Enter")
                case ActionKind.WAIT_FOR | ActionKind.EXTRACT | ActionKind.DIALOG_RESPOND:
                    pass
        except PWTimeout as exc:
            raise ActionError("timeout", _first_line(str(exc))) from exc
        except PWError as exc:
            raise ActionError("not_actionable", _first_line(str(exc))) from exc
        await self._settle()

    async def _settle(self) -> None:
        assert self.page is not None
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=3000)
        except PWError:
            pass

    async def reload(self, *, token: int, actor: str = "automation", timeout_ms: int = 10_000) -> None:
        """Bounded retry primitive for idempotent GET navigations that landed on an error page."""
        self.lease.check(token, actor)
        assert self.page is not None
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        except PWError as exc:
            raise ActionError("timeout", _first_line(str(exc))) from exc

    async def is_obscured(self, resolved: Resolved) -> bool:
        d = await self.describe(resolved)
        return bool(d.get("obscured")) or not bool(d.get("enabled", True))

    async def type_secret(self, resolved: Resolved, secret: str, *, token: int) -> None:
        """Only the login routine calls this. The value is never logged or returned."""
        self.lease.check(token, "automation")
        async with self._automation_input():
            await resolved.element.fill(secret)

    # --- input fence for the headed window -----------------------------------
    async def _set_in_frames(self, name: str, value: Any, frames: list[Any] | None = None) -> None:
        assert self.page is not None
        for frame in frames if frames is not None else self.page.frames:
            try:
                await frame.evaluate("([k, v]) => { window[k] = v; }", [name, value])
            except PWError:
                pass  # detached or navigating: a new document starts with the fence closed

    @contextlib.asynccontextmanager
    async def _automation_input(self) -> AsyncIterator[None]:
        """Open the fence only while the automation dispatches its own (lease-checked) input."""
        await self._set_in_frames("__cuaAuto", True)
        try:
            yield
        finally:
            await self._set_in_frames("__cuaAuto", False)

    async def set_human_input(self, until: float | None) -> None:
        """Open direct manipulation of the headed window to a human until ``until`` (the lease expiry,
        epoch seconds), or close it (None). Called by the handoff manager on every lease change."""
        self._human_until_ms = int(until * 1000) if until else 0
        if self.page is not None:
            await self._set_in_frames("__cuaHumanUntil", self._human_until_ms)

    def _on_frame_navigated(self, frame: Any) -> None:
        if self._human_until_ms > time.time() * 1000:  # a new document starts closed; reopen it for the human in control
            task = asyncio.ensure_future(self._set_in_frames("__cuaHumanUntil", self._human_until_ms, [frame]))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

    async def focused(self) -> Resolved | None:
        """The control that keyboard input would go to (innermost frame first)."""
        for idx, frame, _ in reversed(self._frames()):
            try:
                h = await frame.evaluate_handle(
                    "() => { const a = document.activeElement;"
                    " return a && a !== document.body && !['IFRAME', 'FRAME'].includes(a.tagName) ? a : null; }"
                )
            except PWError:
                continue
            el = h.as_element()
            if el is not None:
                return Resolved(element=el, frame=frame, frame_index=idx, candidate_index=0, strategy="focus")
        return None

    # --- human remote control (operator console) -----------------------------
    async def element_at(self, x: float, y: float) -> Resolved | None:
        assert self.page is not None
        for idx, frame, _ in reversed(self._frames()):
            ox, oy = await self._frame_offset(frame)
            if frame.parent_frame is not None:
                fe = await frame.frame_element()
                box = await fe.bounding_box()
                if not box or not (box["x"] <= x <= box["x"] + box["width"] and box["y"] <= y <= box["y"] + box["height"]):
                    continue
            h = await frame.evaluate_handle(scripts.ELEMENT_AT_POINT, [x - ox, y - oy])
            el = h.as_element()
            if el is not None:
                return Resolved(element=el, frame=frame, frame_index=idx, candidate_index=0, strategy="point")
        return None

    async def raw_click(self, x: float, y: float, *, token: int, actor: str) -> None:
        self.lease.check(token, actor)
        assert self.page is not None
        await self.page.mouse.click(x, y)
        await self._settle()

    async def raw_type(self, text: str, *, token: int, actor: str) -> None:
        self.lease.check(token, actor)
        assert self.page is not None
        await self.page.keyboard.type(text)

    async def raw_press(self, key: str, *, token: int, actor: str) -> None:
        self.lease.check(token, actor)
        assert self.page is not None
        await self.page.keyboard.press(key)
        await self._settle()

    async def state_signature(self) -> dict[str, Any]:
        """Summary of what is blocking/showing on the page (for catalog mining)."""
        from interface_cua.evidence.catalog_mining import SIGNATURE_JS

        assert self.page is not None
        try:
            result: dict[str, Any] = await self.page.main_frame.evaluate(SIGNATURE_JS)
            return result
        except PWError:
            return {"overlay": False, "title": "", "buttons": [], "checkboxes": [], "text": ""}

    async def start_screencast(self, on_frame: Callable[[str], None]) -> None:
        """Live CDP screencast of the page for the operator in control (JPEG frames, base64). Frames are
        handed to the callback and never written anywhere."""
        if self._context is None or self.page is None or self._cdp is not None:
            return
        cdp = await self._context.new_cdp_session(self.page)
        self._cdp = cdp

        async def ack(params: dict[str, Any]) -> None:
            on_frame(params["data"])
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except PWError:
                pass

        cdp.on("Page.screencastFrame", lambda params: asyncio.ensure_future(ack(params)))
        await cdp.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 60, "maxWidth": self.viewport[0], "maxHeight": self.viewport[1], "everyNthFrame": 1},
        )

    async def stop_screencast(self) -> None:
        cdp, self._cdp = self._cdp, None
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            except PWError:
                pass

    async def screenshot_masked(self, input_values: dict[str, str] | None = None) -> bytes:
        assert self.page is not None
        masks = await self._masks(input_values or {})
        return overlay.render(await self.page.screenshot(), masks, None)

    # ------------------------------------------------------------------ reading & conditions
    async def read(self, resolved: Resolved, method: str, attribute: str | None = None) -> str:
        el: ElementHandle = resolved.element
        if method == "input_value":
            return await el.input_value()
        if method == "attribute":
            return (await el.get_attribute(attribute or "value")) or ""
        return _norm(await el.inner_text())

    async def page_texts(self) -> list[str]:
        """Visible text of every frame of the flow (used for sanitized state messages)."""
        out = []
        for _, frame, _ in self._frames():
            try:
                out.append(str(await frame.evaluate(scripts.PAGE_TEXT)))
            except PWError:
                continue
        return out

    async def _region_text(self, cond: TextInRegion, b: Bindings) -> str:
        region = cond.region
        if region.any_frame:
            parts = []
            for _, frame, _ in self._frames():
                try:
                    parts.append(str(await frame.evaluate(scripts.PAGE_TEXT)))
                except PWError:
                    continue
            return _norm(" ".join(parts))
        if region.target is not None:
            try:
                res = await self.resolve(region.target, b)
            except ResolutionError:
                n = await self.count(region.target, b)
                if n == 0:
                    return ""
                found = self._resolve_frame(region.target.frame_path)
                assert found is not None
                texts = []
                for cand in region.target.ordered():
                    for el in await self._candidate_elements(found[0], cand, b):
                        texts.append(_norm(await el.inner_text()))
                    if texts:
                        break
                return " ".join(texts)
            return _norm(await res.element.inner_text())
        found = self._resolve_frame(region.frame_path)
        if found is None:
            return ""
        try:
            return _norm(str(await found[0].evaluate(scripts.PAGE_TEXT)))
        except PWError:
            return ""

    async def evaluate(self, condition: Condition, bindings: Bindings) -> ConditionResult:
        c = condition
        if isinstance(c, AllOf):
            for sub in c.conditions:
                r = await self.evaluate(sub, bindings)
                if not r.holds:
                    return ConditionResult(holds=False, detail=r.detail)
            return ConditionResult(holds=True, detail="all_of holds")
        if isinstance(c, AnyOf):
            details = []
            for sub in c.conditions:
                r = await self.evaluate(sub, bindings)
                if r.holds:
                    return r
                details.append(r.detail)
            return ConditionResult(holds=False, detail="none of: " + "; ".join(details))
        if isinstance(c, Not):
            r = await self.evaluate(c.condition, bindings)
            return ConditionResult(holds=not r.holds, detail=f"not({r.detail})")
        if isinstance(c, UrlMatches):
            found = self._resolve_frame(c.frame_path)
            if found is None:
                return ConditionResult(holds=False, detail="frame not present")
            path = canonicalize(found[0].url).path
            params = match_route(c.route, path)
            if params is None:
                return ConditionResult(holds=False, detail=f"route {path} does not match {c.route}")
            for key, ref in c.params_equal.items():
                if params.get(key) != resolve_value(ref, bindings):
                    return ConditionResult(holds=False, detail=f"route param {key} mismatch")
            return ConditionResult(holds=True, detail=f"route matches {c.route}")
        if isinstance(c, ElementPresent):
            n = await self.count(c.target, bindings)
            return ConditionResult(holds=n >= 1, detail=f"{c.target.description}: {n} match(es)")
        if isinstance(c, ElementAbsent):
            n = await self.count(c.target, bindings)
            return ConditionResult(holds=n == 0, detail=f"{c.target.description}: {n} match(es)")
        if isinstance(c, TextInRegion):
            text = await self._region_text(c, bindings)
            if c.pattern is not None:
                ok = re.search(c.pattern, text, re.IGNORECASE) is not None
                return ConditionResult(holds=ok, detail=f"pattern /{c.pattern}/ {'found' if ok else 'absent'}")
            if c.contains is not None:
                want = _norm(resolve_value(c.contains, bindings))
                ok = bool(want) and want in text
                label = f"input {c.contains.from_input}" if c.contains.from_input else "expected text"
                return ConditionResult(holds=ok, detail=f"{label} {'present' if ok else 'absent'} in region")
            return ConditionResult(holds=bool(text), detail="region has text" if text else "region empty")
        if isinstance(c, FrameLoaded):
            found = self._resolve_frame(c.frame_path)
            if found is None:
                return ConditionResult(holds=False, detail="frame not present")
            path = canonicalize(found[0].url).path
            ok = c.route is None or match_route(c.route, path) is not None
            return ConditionResult(holds=ok, detail=f"frame at {path}")
        if isinstance(c, DialogPresent):
            recent = [e for t, e in self._dialog_history if time.monotonic() - t < 30]
            for e in recent:
                if (c.dialog_type in ("any", e.dialog_type)) and re.search(c.text_pattern, e.message, re.IGNORECASE):
                    return ConditionResult(holds=True, detail=f"dialog seen ({e.response}ed)")
            return ConditionResult(holds=False, detail="no matching dialog")
        if isinstance(c, HttpStatusIs):
            found = self._resolve_frame(c.frame_path)
            status = self._frame_status.get(f"frame:{id(found[0])}") if found else None
            ok = status is not None and str(status).startswith(c.status_class[0])
            return ConditionResult(holds=ok, detail=f"status {status}")
        raise AssertionError(f"unknown condition {c!r}")

    async def wait_for(self, condition: Condition, bindings: Bindings, timeout_ms: int) -> tuple[ConditionResult, int]:
        """Poll an explicit condition until it holds or the timeout elapses (no fixed sleeps)."""
        start = time.monotonic()
        result = await self.evaluate(condition, bindings)
        while not result.holds and (time.monotonic() - start) * 1000 < timeout_ms:
            await asyncio.sleep(0.1)
            try:
                result = await self.evaluate(condition, bindings)
            except PWError as exc:
                result = ConditionResult(holds=False, detail=_first_line(str(exc)))
        return result, int((time.monotonic() - start) * 1000)

    # ------------------------------------------------------------------ evidence
    async def capture_evidence(self, reason: str, directory: Path, input_values: dict[str, str] | None = None) -> list[EvidenceRef]:
        assert self.page is not None
        directory.mkdir(parents=True, exist_ok=True)
        stamp = f"{int(time.time() * 1000) % 10_000_000:07d}"
        refs: list[EvidenceRef] = []
        try:
            png = await self.screenshot_masked(input_values)
            shot = directory / "screenshots" / f"{stamp}-{_slug(reason)}.png"
            shot.parent.mkdir(exist_ok=True)
            shot.write_bytes(png)
            refs.append(EvidenceRef(kind="screenshot", path=self.log.rel(shot), note=f"masked; {reason}"))
        except PWError:
            pass
        parts = []
        for idx, frame, path in self._frames():
            try:
                html = await frame.evaluate(scripts.SANITIZED_DOM, self._sensitive_labels)
            except PWError:
                continue
            where = "/".join(f.name or f.route or "?" for f in path) or "main"
            parts.append(f"<!-- frame {idx}: {where} -->\n{html}")
        dom = directory / "dom" / f"{stamp}-{_slug(reason)}.html"
        dom.parent.mkdir(exist_ok=True)
        dom.write_text(self.redactor.text("\n\n".join(parts)), encoding="utf-8")
        refs.append(EvidenceRef(kind="dom_snapshot", path=self.log.rel(dom), note=f"sanitized; {reason}"))
        return refs


def _rect(r: dict[str, float], ox: float, oy: float) -> Rect:
    return Rect(x=r["x"] + ox, y=r["y"] + oy, w=r["w"], h=r["h"])


def _first_line(s: str) -> str:
    return s.strip().splitlines()[0][:300] if s.strip() else s


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "evidence"


def _cand_detail(c: LocatorCandidate) -> str:
    if isinstance(c, RoleNameLocator):
        return f"role={c.role} name={c.name!r}"
    if isinstance(c, LabelAnchorLocator):
        return f"{c.control} right of label {c.anchor_text!r}"
    if isinstance(c, AttributeLocator):
        return f"{c.tag}{c.attributes}"
    if isinstance(c, TextLocator):
        return f"text {c.text!r}"
    if isinstance(c, StructuralLocator):
        return c.css
    return f"point ({c.x:.0f},{c.y:.0f})"
