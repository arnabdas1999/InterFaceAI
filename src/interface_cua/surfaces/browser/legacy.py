"""Legacy-web adapter: the same contract for frameset-era applications.

Framesets put the application inside a named frame of a shell (banner / navigation / content). The
artifact is surface-neutral: its frame paths, URL conditions, and navigation are *relative to the
application frame*. This adapter anchors them to the configured root frame (``root_frame`` in the
tenant profile), so an artifact recorded on the modern single-page deployment of a product replays
unchanged on a tenant running its classic frameset UI. Masking still covers every frame of the page.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Error as PWError
from playwright.async_api import Frame, Request

from interface_cua.domain.actions import ActionKind, BoundAction
from interface_cua.domain.routes import canonicalize
from interface_cua.domain.targets import FrameRef
from interface_cua.surfaces.base import ActionError, Resolved
from interface_cua.surfaces.browser.adapter import BrowserAdapter, _first_line


class LegacyWebAdapter(BrowserAdapter):
    kind = "web"  # interprets the same web locator candidates

    def __init__(self, *, root_frame: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.root_frame_name = root_frame

    def _root(self) -> Frame:
        assert self.page is not None
        for frame in self.page.frames:
            if frame.name == self.root_frame_name:
                return frame
        return self.page.main_frame  # before the shell has loaded (e.g. the sign-on page)

    def _page_frames(self) -> list[tuple[int, Frame, list[FrameRef]]]:
        return super()._frames()

    def _frames(self) -> list[tuple[int, Frame, list[FrameRef]]]:
        out: list[tuple[int, Frame, list[FrameRef]]] = []

        def walk(frame: Frame, path: list[FrameRef]) -> None:
            out.append((len(out), frame, path))
            for child in frame.child_frames:
                walk(child, [*path, FrameRef(name=child.name or None, route=canonicalize(child.url).path)])

        walk(self._root(), [])
        return out

    def _mask_frames(self) -> list[tuple[int, Frame, list[FrameRef]]]:
        return self._page_frames()  # shell frames (banner with the operator name) are masked too

    def _resolve_frame(self, path: list[FrameRef]) -> tuple[Frame, int] | None:
        frame = self._root()
        for ref in path:
            nxt = next((c for c in frame.child_frames if ref.name and c.name == ref.name), None)
            if nxt is None and ref.route:
                from interface_cua.domain.routes import match_route

                nxt = next((c for c in frame.child_frames if match_route(ref.route, canonicalize(c.url).path) is not None), None)
            if nxt is None:
                return None
            frame = nxt
        idx = next((i for i, f, _ in self._frames() if f is frame), 0)
        return frame, idx

    def _is_own_page(self, request: Request) -> bool:
        """Only navigations of the application frame subtree count as the flow's own navigations."""
        try:
            frame: Frame | None = request.frame
        except PWError:
            return False
        assert self.page is not None
        root = next((f for f in self.page.frames if f.name == self.root_frame_name), None)
        if root is None:  # shell still loading: only the top document is the flow's own navigation
            return frame is self.page.main_frame
        while frame is not None:
            if frame is root:
                return True
            frame = frame.parent_frame
        return False

    async def current_location(self) -> tuple[str, str]:
        cu = canonicalize(self._root().url)
        return cu.origin, cu.path

    async def generator(self) -> str | None:
        return await super().generator()  # the shell document carries the product fingerprint

    async def perform(self, action: BoundAction, resolved: Resolved | None, *, token: int, actor: str, timeout_ms: int = 10_000) -> None:
        if action.kind != ActionKind.NAVIGATE:
            await super().perform(action, resolved, token=token, actor=actor, timeout_ms=timeout_ms)
            return
        self.lease.check(token, actor)
        route = self.profile.route_handles[action.route_handle or ""]
        try:
            await self._root().goto(self.base_url + route, wait_until="domcontentloaded", timeout=timeout_ms)
        except PWError as exc:
            raise ActionError("timeout", _first_line(str(exc))) from exc
        await self._settle()

    async def reload(self, *, token: int, actor: str = "automation", timeout_ms: int = 10_000) -> None:
        self.lease.check(token, actor)
        root = self._root()
        try:
            await root.goto(root.url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PWError as exc:
            raise ActionError("timeout", _first_line(str(exc))) from exc
