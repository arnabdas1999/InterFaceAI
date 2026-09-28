"""Normalized, surface-independent observations. No Playwright types cross this boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.targets import FrameRef


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rect(_Strict):
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)


NameSource = Literal["aria", "label", "content", "value", "title", "placeholder", "adjacent_cell", "none"]


class Control(_Strict):
    """One actionable control, addressed by an ephemeral handle valid for one observation."""

    handle: str
    role: str
    name: str
    name_source: NameSource
    tag: str
    input_type: str | None = None
    value: str | None = None  # masked for the model; see policy.redaction
    frame_index: int = 0
    frame_path: list[FrameRef] = Field(default_factory=list)
    bbox: Rect | None = None
    enabled: bool = True
    checked: bool | None = None
    options: list[str] = Field(default_factory=list)
    href_route: str | None = None
    href_origin: str | None = None
    form_method: str | None = None
    form_action_route: str | None = None
    submits_form: bool = False
    anchor_text: str | None = None  # label text from the adjacent/row-header cell
    attributes: dict[str, str] = Field(default_factory=dict)
    css_path: str | None = None
    sensitive_kind: str | None = None


class FrameInfo(_Strict):
    index: int
    path: list[FrameRef]
    name: str | None
    route: str
    origin: str
    status: int | None = None


class DialogEvent(_Strict):
    dialog_type: str
    message: str
    response: Literal["accept", "dismiss"]
    reason: str
    state_id: str | None = None


class Observation(_Strict):
    seq: int
    route: str
    origin: str
    title: str
    frames: list[FrameInfo]
    controls: list[Control]
    text: dict[int, str]  # frame index -> masked visible text (truncated)
    dialogs: list[DialogEvent] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    generator: str | None = None
    fingerprint: str = ""
    screenshot_path: str | None = None

    def control(self, handle: str) -> Control | None:
        return next((c for c in self.controls if c.handle == handle), None)
