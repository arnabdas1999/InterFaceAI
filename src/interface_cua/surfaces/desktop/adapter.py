"""Desktop adapter: the same surface contract over Windows UI Automation (UIA).

Perception is the UIA tree (the desktop counterpart of the accessibility tree) plus a masked
set-of-marks screenshot of the application window. Unnamed controls get their label from the static
text to their left - the desktop form of the web adapter's adjacent-cell rule. Locator candidates are
tagged ``desktop-uia``: control type + name, label-anchor (by geometry), and AutomationId.

Screens map to routes (``/desktop/member-inquiry`` from the window title), so policy, conditions and
artifacts need no desktop-specific concepts. Every action still presents the control-lease token.
Windows only; the module imports ``uiautomation`` lazily.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PIL import Image

from interface_cua.domain.actions import ActionKind, BoundAction
from interface_cua.domain.conditions import (
    AllOf,
    AnyOf,
    Condition,
    ConditionResult,
    ElementAbsent,
    ElementPresent,
    FrameLoaded,
    Not,
    TextInRegion,
    UrlMatches,
)
from interface_cua.domain.observations import Control, DialogEvent, FrameInfo, Observation, Rect
from interface_cua.domain.profiles import AppProfile, TenantProfile
from interface_cua.domain.results import EvidenceRef, LocatorDiagnostic
from interface_cua.domain.routes import match_route
from interface_cua.domain.targets import (
    SCORES,
    AttributeLocator,
    LabelAnchorLocator,
    LocatorCandidate,
    RoleNameLocator,
    TargetSpec,
)
from interface_cua.evidence.logger import RunLog, opaque_id
from interface_cua.handoff.control_lease import ControlLease
from interface_cua.policy.engine import PolicyEngine
from interface_cua.policy.redaction import Redactor
from interface_cua.surfaces.base import ActionError, Bindings, ResolutionError, Resolved, SessionHandle
from interface_cua.surfaces.browser import overlay
from interface_cua.surfaces.browser.adapter import resolve_value, vocab_text

ROLE_BY_TYPE = {
    "EditControl": "textbox",
    "ButtonControl": "button",
    "ComboBoxControl": "combobox",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
    "HyperlinkControl": "link",
}
CONTROL_BY_ROLE = {"textbox": "input", "combobox": "select", "button": "button", "checkbox": "checkbox", "link": "link"}
KIND = "desktop-uia"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _strip(s: str) -> str:
    return " ".join((s or "").split()).rstrip(":").strip()


def _print_window(hwnd: int) -> tuple[Image.Image, tuple[int, int]]:
    """Render a window into an off-screen bitmap. Works while the window is covered; fails closed."""
    if sys.platform != "win32":  # Windows-only; also tells type checkers on other platforms
        raise OSError("the desktop adapter requires Windows")
    import ctypes
    from ctypes import wintypes

    user32, gdi32 = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
    vp = ctypes.c_void_p
    user32.GetWindowDC.restype, user32.GetWindowDC.argtypes = vp, [vp]
    user32.ReleaseDC.argtypes = [vp, vp]
    user32.PrintWindow.argtypes = [vp, vp, wintypes.UINT]
    user32.GetWindowRect.argtypes = [vp, ctypes.POINTER(wintypes.RECT)]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = vp, [vp]
    gdi32.CreateCompatibleBitmap.restype, gdi32.CreateCompatibleBitmap.argtypes = vp, [vp, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = vp, [vp, vp]
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.GetDIBits.argtypes = [vp, vp, wintypes.UINT, wintypes.UINT, vp, vp, wintypes.UINT]

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise ActionError("not_actionable", "application window has no area (minimized?)")
    hdc_win = user32.GetWindowDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, hdc, 2):  # PW_RENDERFULLCONTENT
            raise ActionError("not_actionable", "application window could not be captured")
        header = (ctypes.c_uint32 * 10)(40, w, (-h) & 0xFFFFFFFF, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)  # BITMAPINFOHEADER, top-down
        pixels = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(hdc, bmp, 0, h, pixels, header, 0)
    finally:
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(hwnd, hdc_win)
    img = Image.frombuffer("RGB", (w, h), pixels.raw, "raw", "BGRX", 0, 1)
    # A DPI-unaware window renders at its logical size; the window rect and UIA rects are physical pixels.
    user32.GetDpiForWindow.argtypes = [vp]
    user32.MonitorFromWindow.restype, user32.MonitorFromWindow.argtypes = vp, [vp, wintypes.DWORD]
    mon_dpi, _y = wintypes.UINT(96), wintypes.UINT(96)
    ctypes.WinDLL("shcore").GetDpiForMonitor(ctypes.c_void_p(user32.MonitorFromWindow(hwnd, 2)), 0, ctypes.byref(mon_dpi), ctypes.byref(_y))
    scale = mon_dpi.value / (user32.GetDpiForWindow(hwnd) or 96)
    if abs(scale - 1) > 0.01:
        img = img.crop((0, 0, round(w / scale), round(h / scale))).resize((w, h), Image.Resampling.LANCZOS)
    return img, (rect.left, rect.top)


class DesktopAdapter:
    kind = KIND

    def __init__(
        self,
        *,
        tenant: TenantProfile,
        profile: AppProfile,
        policy: PolicyEngine,
        redactor: Redactor,
        log: RunLog,
        lease: ControlLease,
        base_url: str,
        headed: bool = True,
        trace: bool = False,
        viewport: tuple[int, int] = (1280, 800),
    ) -> None:
        import uiautomation as auto  # Windows only

        self.auto = auto
        self.tenant = tenant
        self.profile = profile
        self.policy = policy
        self.redactor = redactor
        self.log = log
        self.lease = lease
        self.base_url = base_url.rstrip("/")
        self.origin = base_url.rstrip("/")
        self.viewport = viewport
        self.trace_enabled = False  # no Playwright trace on desktop; evidence is screenshots + UIA dumps
        self.session_id = opaque_id("sess")
        self.context_id = opaque_id("proc")
        self.dialog_decider: Any = None
        self.human_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None
        self.page = None  # no browser page
        self.routes_seen: list[str] = []
        self.blocked_navigations: list[str] = []
        self._proc: subprocess.Popen[bytes] | None = None
        self._win: Any = None
        self._handles: dict[str, Any] = {}
        self._sensitive_labels = [(re.compile(lbl.label_pattern, re.I), lbl.kind) for lbl in profile.sensitive_fields.labels]
        self._sensitive_patterns = [(re.compile(p.pattern), p.kind) for p in profile.sensitive_fields.patterns]

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        launch = list(self.tenant.desktop_launch or [])
        if not launch:
            raise ActionError("not_actionable", "tenant profile has no desktop_launch command")
        self._com = self.auto.UIAutomationInitializerInThread()  # COM must be initialized on this thread
        fault = os.environ.get("CUA_DESKTOP_FAULT")  # out-of-band fault injection for tests/demos
        if fault:
            launch += ["-Fault", fault]
        self._proc = subprocess.Popen(launch, cwd=str(Path(__file__).resolve().parents[4]))
        title = self.tenant.window_title or ""
        deadline = time.monotonic() + 20
        # Attach to the window of the process we launched, never to any window with a matching title:
        # another instance (or an unrelated app) must not receive our actions.
        while (win := self._own_window(title)) is None:  # UIA is COM: stay on the thread that initialized it
            if time.monotonic() > deadline:
                raise ActionError("timeout", f"desktop window '{title}' did not appear")
            await asyncio.sleep(0.25)
        self._win = win
        self._win.SetActive()
        self.log.session_id = self.session_id
        self.log.emit(
            "session_started",
            "desktop application session opened",
            session_id=self.session_id,
            context_id=self.context_id,
            process=self._proc.pid,
            surface=KIND,
        )

    def _own_window(self, title: str) -> Any:
        assert self._proc is not None
        for w in self.auto.GetRootControl().GetChildren():
            if w.ProcessId == self._proc.pid and title in (w.Name or ""):
                return w
        return None

    async def start_trace(self) -> None:
        return None

    async def close(self, trace_path: Path | None = None) -> None:
        if self._proc is not None:
            subprocess.run(["taskkill", "/PID", str(self._proc.pid), "/T", "/F"], capture_output=True, check=False)
            self._proc = None

    def session_handle(self) -> SessionHandle:
        return SessionHandle(session_id=self.session_id, context_id=self.context_id, route=self._route())

    # ------------------------------------------------------------------ screen / location
    def _title(self) -> str:
        return str(self._win.Name) if self._win is not None and self._win.Exists(0, 0) else ""

    def _route(self) -> str:
        title = self._title()
        screen = title.split(" - ", 1)[1] if " - " in title else title
        return "/desktop/" + _slug(screen)

    async def current_location(self) -> tuple[str, str]:
        return self.origin, self._route()

    async def generator(self) -> str | None:
        title = self._title()
        return title.split(" - ", 1)[0] if title else None

    def _win_rect(self) -> tuple[int, int, int, int]:
        """Visible window frame. The plain window rectangle includes invisible resize borders, so a capture
        of it would include pixels of whatever window is behind the application."""
        if sys.platform != "win32":  # Windows-only; also tells type checkers on other platforms
            raise OSError("the desktop adapter requires Windows")
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        hwnd = wintypes.HWND(self._win.NativeWindowHandle)
        dwmwa_extended_frame_bounds = 9
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, dwmwa_extended_frame_bounds, ctypes.byref(rect), ctypes.sizeof(rect))
        if hr == 0:
            return rect.left, rect.top, rect.right, rect.bottom
        r = self._win.BoundingRectangle
        return r.left, r.top, r.right, r.bottom

    def _controls(self) -> list[Any]:
        """Application controls (the window's own title bar and system buttons are not part of the app)."""
        out = []
        for c, _depth in self.auto.WalkControl(self._win, maxDepth=4):
            if c.ControlTypeName in {"TitleBarControl", "MenuBarControl"}:
                continue
            parent = c.GetParentControl()
            if parent is not None and parent.ControlTypeName in {"TitleBarControl", "MenuBarControl"}:
                continue
            r = c.BoundingRectangle
            if r.width() <= 0 or r.height() <= 0:
                continue
            out.append(c)
        return out

    def _texts(self, controls: list[Any]) -> list[Any]:
        return [c for c in controls if c.ControlTypeName == "TextControl" and _strip(c.Name)]

    def _label_left_of(self, ctrl: Any, texts: list[Any]) -> str | None:
        r = ctrl.BoundingRectangle
        mid = (r.top + r.bottom) / 2
        best, dist = None, None
        for t in texts:
            tr = t.BoundingRectangle
            if tr.right <= r.left + 2 and tr.top - 4 <= mid <= tr.bottom + 4:
                d = r.left - tr.right
                if dist is None or d < dist:
                    best, dist = t, d
        return _strip(best.Name) if best is not None else None

    def _value_right_of(self, label: Any, texts: list[Any]) -> Any | None:
        lr = label.BoundingRectangle
        mid = (lr.top + lr.bottom) / 2
        cands = [
            t
            for t in texts
            if t is not label
            and t.BoundingRectangle.left >= lr.right - 2
            and t.BoundingRectangle.top - 4 <= mid <= t.BoundingRectangle.bottom + 4
        ]
        return min(cands, key=lambda t: t.BoundingRectangle.left) if cands else None

    def _describe(self, c: Any, texts: list[Any]) -> dict[str, Any]:
        role = ROLE_BY_TYPE.get(c.ControlTypeName, "text" if c.ControlTypeName == "TextControl" else "generic")
        name = _strip(c.Name)
        source = "label" if name else "none"
        anchor = None
        if role == "textbox" or not name:
            anchor = self._label_left_of(c, texts)
            if not name and anchor:
                name, source = anchor, "adjacent_cell"
        value = None
        if role == "textbox":
            try:
                value = c.GetValuePattern().Value
            except Exception:
                value = None
        aid = str(c.AutomationId or "")
        return {
            "role": role,
            "name": name,
            "name_source": source,
            "anchor_text": anchor,
            "automation_id": aid if aid and not aid.isdigit() else "",
            "type": c.ControlTypeName,
            "value": value,
            "enabled": bool(c.IsEnabled),
        }

    # ------------------------------------------------------------------ perception
    def _rel(self, c: Any) -> Rect:
        wl, wt, _, _ = self._win_rect()
        r = c.BoundingRectangle
        return Rect(x=r.left - wl, y=r.top - wt, w=r.width(), h=r.height())

    def _masks(self, controls: list[Any], input_values: dict[str, str]) -> list[tuple[Rect, str]]:
        texts = self._texts(controls)
        masks: list[tuple[Rect, str]] = []
        for t in texts:
            label = _strip(t.Name)
            for rx, kind in self._sensitive_labels:
                if rx.search(label):
                    v = self._value_right_of(t, texts)
                    if v is not None:
                        if self.redactor.input_ref(_strip(v.Name)) is None:
                            self.redactor.add_value(_strip(v.Name), kind)
                        masks.append((self._rel(v), kind))
                    break
            for rx, kind in self._sensitive_patterns:
                if rx.search(t.Name or ""):
                    masks.append((self._rel(t), kind))
            for name, value in input_values.items():
                if value and len(value) >= 3 and re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", t.Name or ""):
                    masks.append((self._rel(t), f"input:{name}"))
        for c in controls:
            if c.ControlTypeName == "EditControl":
                try:
                    v = c.GetValuePattern().Value
                except Exception:
                    v = ""
                ref = self.redactor.input_ref(v) if v else None
                if ref:
                    masks.append((self._rel(c), f"input:{ref}"))
        return masks

    def _grab(self) -> bytes:
        """The application window's own pixels (PrintWindow), never a screen region: a screen grab of a
        window that is behind another app would capture that app's content into evidence and model input."""
        hwnd = self._win.NativeWindowHandle
        full, (wl, wt) = _print_window(hwnd)
        left, top, right, bottom = self._win_rect()
        img = full.crop((left - wl, top - wt, right - wl, bottom - wt))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def observe(
        self, *, seq: int, save_to: Path | None = None, input_values: dict[str, str] | None = None
    ) -> tuple[Observation, bytes]:
        input_values = input_values or {}
        await asyncio.sleep(0.15)  # let the UI thread repaint after the last action (bounded, not a wait-for-state)
        controls_raw = self._controls()
        texts = self._texts(controls_raw)
        masks = self._masks(controls_raw, input_values)
        controls: list[Control] = []
        self._handles = {}
        n = 0
        for c in controls_raw:
            if c.ControlTypeName not in ROLE_BY_TYPE:
                continue
            d = self._describe(c, texts)
            n += 1
            handle = str(n)
            self._handles[handle] = c
            value = d["value"]
            controls.append(
                Control(
                    handle=handle,
                    role=d["role"],
                    name=self.redactor.for_model(d["name"])[:120],
                    name_source=d["name_source"],
                    tag=d["type"],
                    value=self.redactor.for_model(value) if value else None,
                    bbox=self._rel(c),
                    enabled=d["enabled"],
                    anchor_text=d["anchor_text"],
                    attributes={"automation_id": d["automation_id"]} if d["automation_id"] else {},
                )
            )
        ordered = sorted(texts, key=lambda t: (round(t.BoundingRectangle.top / 10), t.BoundingRectangle.left))
        lines: list[str] = []
        for t in ordered:
            if lines and ordered.index(t) > 0 and abs(t.BoundingRectangle.top - ordered[ordered.index(t) - 1].BoundingRectangle.top) < 10:
                lines[-1] += "\t" + t.Name
            else:
                lines.append(t.Name)
        route = self._route()
        fp = hashlib.sha256((route + "|".join(f"{c.role}:{c.name}:{c.value}" for c in controls) + "|".join(lines)).encode()).hexdigest()[
            :16
        ]
        obs = Observation(
            seq=seq,
            route=route,
            origin=self.origin,
            title=self.redactor.for_model(self._title()),
            frames=[FrameInfo(index=0, path=[], name=None, route=route, origin=self.origin)],
            controls=controls,
            text={0: self.redactor.for_model("\n".join(lines))[:4000]},
            generator=await self.generator(),
            fingerprint=fp,
        )
        png = overlay.render(self._grab(), masks, controls)
        if save_to is not None:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(png)
            obs = obs.model_copy(update={"screenshot_path": save_to.name})
        return obs, png

    def handle(self, handle: str) -> Resolved | None:
        c = self._handles.get(handle)
        return None if c is None else Resolved(element=c, frame=None, frame_index=0, candidate_index=0, strategy="handle")

    # ------------------------------------------------------------------ targeting
    def _candidate_controls(self, cand: LocatorCandidate, b: Bindings) -> list[Any]:
        controls = self._controls()
        if isinstance(cand, RoleNameLocator):
            name = vocab_text(cand.name, b)
            return [c for c in controls if ROLE_BY_TYPE.get(c.ControlTypeName) == cand.role and _strip(c.Name) == _strip(name)]
        if isinstance(cand, LabelAnchorLocator):
            texts = self._texts(controls)
            anchor = _strip(vocab_text(cand.anchor_text, b)).lower()
            labels = [t for t in texts if _strip(t.Name).lower() == anchor]
            out = []
            for lbl in labels:
                if cand.control == "cell":
                    v = self._value_right_of(lbl, texts)
                    if v is not None:
                        out.append(v)
                    continue
                role = {v: k for k, v in CONTROL_BY_ROLE.items()}.get(cand.control)
                row = [
                    c for c in controls if ROLE_BY_TYPE.get(c.ControlTypeName) == role and self._label_left_of(c, texts) == _strip(lbl.Name)
                ]
                out.extend(row[:1])
            return out
        if isinstance(cand, AttributeLocator):
            aid = cand.attributes.get("automation_id")
            return [c for c in controls if aid and str(c.AutomationId) == aid]
        return []

    async def resolve(self, target: TargetSpec, bindings: Bindings) -> Resolved:
        diags: list[LocatorDiagnostic] = []
        for rank, cand in enumerate(target.ordered()):
            if KIND not in cand.adapter_kinds:
                diags.append(LocatorDiagnostic(strategy=cand.strategy, match_count=-1, detail="not interpretable by desktop adapter"))
                continue
            found = self._candidate_controls(cand, bindings)
            diags.append(LocatorDiagnostic(strategy=cand.strategy, match_count=len(found)))
            if len(found) == 1:
                return Resolved(
                    element=found[0], frame=None, frame_index=0, candidate_index=rank, strategy=cand.strategy, diagnostics=diags
                )
            if len(found) > 1:
                raise ResolutionError("locator_ambiguous", f"{cand.strategy} matched {len(found)} controls", diags)
        raise ResolutionError("locator_not_found", f"no candidate matched: {target.description}", diags)

    async def count(self, target: TargetSpec, bindings: Bindings) -> int:
        for cand in target.ordered():
            if KIND in cand.adapter_kinds:
                n = len(self._candidate_controls(cand, bindings))
                if n:
                    return n
        return 0

    async def describe(self, resolved: Resolved) -> dict[str, Any]:
        return self._describe(resolved.element, self._texts(self._controls()))

    async def build_target(self, resolved: Resolved, description: str, bindings: Bindings) -> TargetSpec:
        d = await self.describe(resolved)
        cands: list[LocatorCandidate] = []
        if d["name"] and d["name_source"] == "label" and d["role"] not in {"generic", "text"}:
            cands.append(
                RoleNameLocator(
                    role=d["role"],
                    name=d["name"],
                    adapter_kinds=[KIND],
                    score=SCORES["role_name"],
                    rationale=f"UIA control type {d['type']} with name '{d['name']}'.",
                )
            )
        if d["anchor_text"] and d["role"] in CONTROL_BY_ROLE:
            cands.append(
                LabelAnchorLocator(
                    anchor_text=d["anchor_text"],
                    control=CONTROL_BY_ROLE[d["role"]],
                    adapter_kinds=[KIND],
                    score=SCORES["role_name_inferred"],
                    rationale="Unnamed control; label is the static text to its left.",
                )
            )
        if d["automation_id"]:
            cands.append(
                AttributeLocator(
                    tag=d["type"],
                    attributes={"automation_id": d["automation_id"]},
                    adapter_kinds=[KIND],
                    score=SCORES["attribute"],
                    rationale="Stable UIA AutomationId (the form's control name).",
                )
            )
        target_id = resolved.element.GetRuntimeId()
        verified = [c for c in cands if [x.GetRuntimeId() for x in self._candidate_controls(c, bindings)] == [target_id]]
        if not verified:
            raise ResolutionError("locator_not_found", "control has no stable identity", [])
        return TargetSpec(description=description, candidates=verified)

    async def bind(self, action: BoundAction, resolved: Resolved | None) -> BoundAction:
        if resolved is None:
            return action
        d = await self.describe(resolved)
        return action.model_copy(update={"element_role": d["role"], "element_name": d["name"]})

    # ------------------------------------------------------------------ acting
    async def perform(self, action: BoundAction, resolved: Resolved | None, *, token: int, actor: str, timeout_ms: int = 10_000) -> None:
        self.lease.check(token, actor)
        c = resolved.element if resolved else None
        try:
            match action.kind:
                case ActionKind.NAVIGATE:
                    route = self.profile.route_handles[action.route_handle or ""]
                    if match_route(route, self._route()) is None:
                        raise ActionError("not_actionable", f"desktop app is on {self._route()}, cannot navigate to {route}")
                case ActionKind.CLICK:
                    assert c is not None
                    try:
                        c.GetInvokePattern().Invoke()
                    except Exception:
                        self._ensure_foreground()
                        c.Click(simulateMove=False)
                case ActionKind.TYPE:
                    assert c is not None
                    await self._set_value(c, action.value or "")
                case ActionKind.SELECT:
                    assert c is not None
                    c.Select(action.value or "")
                case ActionKind.PRESS:
                    self._ensure_foreground()
                    if c is not None:
                        c.SetFocus()
                    self.auto.SendKeys("{" + (action.key or "Enter") + "}")
                case ActionKind.WAIT_FOR | ActionKind.EXTRACT | ActionKind.DIALOG_RESPOND:
                    pass
        except ActionError:
            raise
        except Exception as exc:
            raise ActionError("not_actionable", f"{type(exc).__name__}: {exc}") from exc
        await asyncio.sleep(0.1)

    async def reload(self, *, token: int, actor: str = "automation", timeout_ms: int = 10_000) -> None:
        raise ActionError("not_actionable", "desktop screens cannot be reloaded; retry is not available")

    async def is_obscured(self, resolved: Resolved) -> bool:
        return not bool(resolved.element.IsEnabled)

    def _ensure_foreground(self) -> None:
        """Synthesized mouse/keyboard input goes to whichever window is in front. Refuse rather than send it
        to another application; pattern-based actions (Invoke, SetValue) do not need this."""
        if sys.platform != "win32":  # Windows-only; also tells type checkers on other platforms
            raise OSError("the desktop adapter requires Windows")
        import ctypes

        self._win.SetActive()
        if ctypes.windll.user32.GetForegroundWindow() != self._win.NativeWindowHandle:
            raise ActionError("not_actionable", "application window is not in the foreground; synthesized input refused")

    async def _set_value(self, c: Any, value: str) -> None:
        """Set and read back: a value that did not take must fail here, not surface later as a wrong search."""
        for _ in range(3):
            c.GetValuePattern().SetValue(value)
            if c.IsPassword:
                return  # a password box does not expose its value; nothing to read back
            await asyncio.sleep(0.05)
            if str(c.GetValuePattern().Value) == value:
                return
        raise ActionError("not_actionable", "value did not take in the control (read-back mismatch)")  # never echo the value

    async def type_secret(self, resolved: Resolved, secret: str, *, token: int) -> None:
        self.lease.check(token, "automation")
        await self._set_value(resolved.element, secret)

    async def set_human_input(self, until: float | None) -> None:
        """A native window cannot be fenced from outside the application: on the desktop, operator input
        goes through the lease-checked operator API (raw_* below), never a fenced headed window."""
        return None

    async def focused(self) -> Resolved | None:
        c = self.auto.GetFocusedControl()
        if c is None or self._proc is None or c.ProcessId != self._proc.pid:
            return None  # focus is in another application: nothing of ours would receive the keys
        return Resolved(element=c, frame=None, frame_index=0, candidate_index=0, strategy="focus")

    async def element_at(self, x: float, y: float) -> Resolved | None:
        wl, wt, _, _ = self._win_rect()
        c = self.auto.ControlFromPoint(int(wl + x), int(wt + y))
        return None if c is None else Resolved(element=c, frame=None, frame_index=0, candidate_index=0, strategy="point")

    async def raw_click(self, x: float, y: float, *, token: int, actor: str) -> None:
        self.lease.check(token, actor)
        self._ensure_foreground()
        wl, wt, _, _ = self._win_rect()
        self.auto.Click(int(wl + x), int(wt + y))

    async def raw_type(self, text: str, *, token: int, actor: str) -> None:
        self.lease.check(token, actor)
        self._ensure_foreground()
        self.auto.SendKeys(text, interval=0.01)

    async def raw_press(self, key: str, *, token: int, actor: str) -> None:
        self.lease.check(token, actor)
        self._ensure_foreground()
        self.auto.SendKeys("{" + key + "}")

    async def screenshot_masked(self, input_values: dict[str, str] | None = None) -> bytes:
        return overlay.render(self._grab(), self._masks(self._controls(), input_values or {}), None)

    async def start_screencast(self, on_frame: Callable[[str], None]) -> None:
        return None  # production: a desktop streaming agent; the console falls back to snapshots

    async def stop_screencast(self) -> None:
        return None

    async def state_signature(self) -> dict[str, Any]:
        texts = [_strip(t.Name) for t in self._texts(self._controls())]
        buttons = [_strip(c.Name) for c in self._controls() if c.ControlTypeName == "ButtonControl" and _strip(c.Name)]
        return {"overlay": False, "title": self._title(), "buttons": buttons[:8], "checkboxes": [], "text": " | ".join(texts)[:600]}

    def drain_dialogs(self) -> list[DialogEvent]:
        return []

    def drain_blocked(self) -> list[str]:
        return []

    # ------------------------------------------------------------------ reading & conditions
    async def read(self, resolved: Resolved, method: str, attribute: str | None = None) -> str:
        c = resolved.element
        if method == "input_value" or c.ControlTypeName == "EditControl":
            return str(c.GetValuePattern().Value)
        return " ".join(str(c.Name).split())

    async def page_texts(self) -> list[str]:
        return [str(t.Name) for t in self._texts(self._controls())]

    async def _region_text(self, cond: TextInRegion, b: Bindings) -> str:
        if cond.region.target is not None:
            try:
                res = await self.resolve(cond.region.target, b)
            except ResolutionError:
                return ""
            return await self.read(res, "text")
        return " ".join(" ".join(await self.page_texts()).split())

    async def evaluate(self, condition: Condition, bindings: Bindings) -> ConditionResult:
        c = condition
        if isinstance(c, AllOf):
            for sub in c.conditions:
                r = await self.evaluate(sub, bindings)
                if not r.holds:
                    return r
            return ConditionResult(holds=True, detail="all_of holds")
        if isinstance(c, AnyOf):
            for sub in c.conditions:
                r = await self.evaluate(sub, bindings)
                if r.holds:
                    return r
            return ConditionResult(holds=False, detail="none of the alternatives hold")
        if isinstance(c, Not):
            r = await self.evaluate(c.condition, bindings)
            return ConditionResult(holds=not r.holds, detail=f"not({r.detail})")
        if isinstance(c, UrlMatches):
            ok = not c.frame_path and match_route(c.route, self._route()) is not None
            return ConditionResult(holds=ok, detail=f"screen {self._route()}")
        if isinstance(c, ElementPresent | ElementAbsent):
            n = await self.count(c.target, bindings)
            present = n >= 1
            return ConditionResult(holds=present if isinstance(c, ElementPresent) else not present, detail=f"{c.target.description}: {n}")
        if isinstance(c, TextInRegion):
            text = await self._region_text(c, bindings)
            if c.pattern is not None:
                ok = re.search(c.pattern, text, re.I) is not None
                return ConditionResult(holds=ok, detail=f"pattern /{c.pattern}/ {'found' if ok else 'absent'}")
            if c.contains is not None:
                want = " ".join(resolve_value(c.contains, bindings).split())
                ok = bool(want) and want in text
                return ConditionResult(holds=ok, detail="expected text present" if ok else "expected text absent")
            return ConditionResult(holds=bool(text), detail="region text")
        if isinstance(c, FrameLoaded):
            return ConditionResult(holds=not c.frame_path, detail="desktop screens have no frames")
        return ConditionResult(holds=False, detail=f"{c.kind} is not observable on the desktop surface")

    async def wait_for(self, condition: Condition, bindings: Bindings, timeout_ms: int) -> tuple[ConditionResult, int]:
        start = time.monotonic()
        result = await self.evaluate(condition, bindings)
        while not result.holds and (time.monotonic() - start) * 1000 < timeout_ms:
            await asyncio.sleep(0.15)
            result = await self.evaluate(condition, bindings)
        return result, int((time.monotonic() - start) * 1000)

    # ------------------------------------------------------------------ evidence
    async def capture_evidence(self, reason: str, directory: Path, input_values: dict[str, str] | None = None) -> list[EvidenceRef]:
        stamp = f"{int(time.time() * 1000) % 10_000_000:07d}"
        slug = _slug(reason)[:40] or "evidence"
        shot = directory / "screenshots" / f"{stamp}-{slug}.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(await self.screenshot_masked(input_values))
        texts = self._texts(self._controls())
        tree = [
            {
                "type": c.ControlTypeName,
                "name": self.redactor.text(str(c.Name)),
                "automation_id": str(c.AutomationId),
                "rect": self._rel(c).model_dump(),
            }
            for c in self._controls()
        ]
        for node, c in zip(tree, self._controls(), strict=False):  # label-mapped values are masked in the dump too
            if c.ControlTypeName == "TextControl":
                for t in texts:
                    v = self._value_right_of(t, texts)
                    if (
                        v is not None
                        and v.GetRuntimeId() == c.GetRuntimeId()
                        and any(rx.search(_strip(t.Name)) for rx, _ in self._sensitive_labels)
                    ):
                        node["name"] = "⟦masked⟧"
        dump = directory / "dom" / f"{stamp}-{slug}.uia.json"
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(
            json.dumps({"window": self.redactor.text(self._title()), "controls": tree}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return [
            EvidenceRef(kind="screenshot", path=self.log.rel(shot), note=f"masked; {reason}"),
            EvidenceRef(kind="dom_snapshot", path=self.log.rel(dump), note=f"sanitized UIA tree; {reason}"),
        ]
