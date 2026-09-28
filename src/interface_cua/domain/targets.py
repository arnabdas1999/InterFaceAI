"""How a control is identified: frame path + an ordered bundle of locator candidates.

Candidates are surface-neutral descriptions tagged with the adapter kinds that can interpret them
(``web`` today; ``desktop-uia`` / ``legacy-web`` / ``visual`` are documented extension points).
The model never writes these; the adapter records them from the live element at action time.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# Fixed robustness rubric (ADR 002). Not learned; reviewers can reason about it.
SCORES = {
    "role_name": 0.9,
    "role_name_inferred": 0.85,
    "label_anchor": 0.8,
    "attribute": 0.7,
    "text": 0.6,
    "structural": 0.3,
    "coordinate": 0.1,
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrameRef(_Strict):
    """One hop into a nested frame/window. Matched by name first, then by route pattern."""

    name: str | None = None
    route: str | None = None


class _CandidateBase(_Strict):
    adapter_kinds: list[str] = Field(default_factory=lambda: ["web"])
    expected_count: int = 1
    score: float
    rationale: str


class RoleNameLocator(_CandidateBase):
    """Accessibility role + accessible name. Name may be a vocabulary key: ``vocab:share_savings``."""

    strategy: Literal["role_name"] = "role_name"
    role: str
    name: str
    exact: bool = True


class LabelAnchorLocator(_CandidateBase):
    """A control located relative to visible label text, e.g. the input in the table row whose
    header cell reads "Member ID:". Works on legacy markup that has no <label> or aria wiring.
    ``control="cell"`` targets the value cell itself (used by extractors and checkpoints)."""

    strategy: Literal["label_anchor"] = "label_anchor"
    anchor_text: str  # may be vocab:key; matched after trimming and dropping a trailing colon
    relation: Literal["same_row_following"] = "same_row_following"
    control: Literal["input", "select", "button", "link", "checkbox", "cell"]
    input_type: str | None = None


class AttributeLocator(_CandidateBase):
    strategy: Literal["attribute"] = "attribute"
    tag: str
    attributes: dict[str, str]


class TextLocator(_CandidateBase):
    strategy: Literal["text"] = "text"
    text: str
    tag: str | None = None


class StructuralLocator(_CandidateBase):
    strategy: Literal["structural"] = "structural"
    css: str


class CoordinateLocator(_CandidateBase):
    """Last resort for surfaces with no addressable controls. Always flagged for review."""

    strategy: Literal["coordinate"] = "coordinate"
    x: float
    y: float
    viewport: tuple[int, int]


LocatorCandidate = Annotated[
    RoleNameLocator | LabelAnchorLocator | AttributeLocator | TextLocator | StructuralLocator | CoordinateLocator,
    Field(discriminator="strategy"),
]


class TargetSpec(_Strict):
    description: str
    frame_path: list[FrameRef] = Field(default_factory=list)
    candidates: list[LocatorCandidate]
    review_required: bool = False

    def ordered(self) -> list[LocatorCandidate]:
        order = list(SCORES)
        return sorted(
            self.candidates,
            key=lambda c: (-c.score, order.index(c.strategy) if c.strategy in order else 99),
        )


class RegionSpec(_Strict):
    """A region of a frame used for text checks: either a target or the whole frame body."""

    frame_path: list[FrameRef] = Field(default_factory=list)
    target: TargetSpec | None = None
    any_frame: bool = False  # global detectors look at every frame (e.g. an error inside an iframe)
