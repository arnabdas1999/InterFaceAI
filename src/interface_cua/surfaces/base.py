"""The seam between "how we perceive/act on a surface" and "the recorded flow".

Discovery, replay, the compiler, and the handoff layer depend only on this contract. The browser
adapter implements it with Playwright; a desktop adapter would implement it with UI Automation +
screenshots + OS input, interpreting the ``desktop-uia`` locator candidates in the same bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from interface_cua.domain.actions import BoundAction
from interface_cua.domain.conditions import Condition, ConditionResult
from interface_cua.domain.observations import DialogEvent, Observation
from interface_cua.domain.results import EvidenceRef, LocatorDiagnostic
from interface_cua.domain.targets import TargetSpec


@dataclass(frozen=True)
class SessionHandle:
    session_id: str
    context_id: str
    route: str


@dataclass
class Resolved:
    """A uniquely resolved live control. ``element`` is adapter-private."""

    element: Any
    frame: Any
    frame_index: int
    candidate_index: int
    strategy: str
    diagnostics: list[LocatorDiagnostic] = field(default_factory=list)


class ResolutionError(Exception):
    def __init__(self, code: str, message: str, diagnostics: list[LocatorDiagnostic]):
        super().__init__(message)
        self.code = code  # locator_not_found | locator_ambiguous | frame_not_found
        self.diagnostics = diagnostics


class ActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # timeout | not_actionable | stale_lease | blocked


@dataclass
class Bindings:
    """Values available to conditions/actions at run time. Secrets are never in here."""

    inputs: dict[str, str]
    vocab: dict[str, str]
    base_url: str


class SurfaceAdapter(Protocol):
    kind: str

    async def observe(self, *, seq: int, save_to: str | None) -> tuple[Observation, bytes]: ...

    async def resolve(self, target: TargetSpec, bindings: Bindings) -> Resolved: ...

    async def bind(self, action: BoundAction, resolved: Resolved | None) -> BoundAction: ...

    async def perform(self, action: BoundAction, resolved: Resolved | None, *, token: int, actor: str) -> None: ...

    async def evaluate(self, condition: Condition, bindings: Bindings) -> ConditionResult: ...

    async def read(self, resolved: Resolved, method: str, attribute: str | None = None) -> str: ...

    async def capture_evidence(self, reason: str, directory: str) -> list[EvidenceRef]: ...

    def drain_dialogs(self) -> list[DialogEvent]: ...

    def session_handle(self) -> SessionHandle: ...
