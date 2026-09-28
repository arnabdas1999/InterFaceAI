"""Closed, deterministic condition vocabulary for preconditions, postconditions, checkpoints, and
state detectors. No free-form code can appear in an artifact."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.targets import FrameRef, RegionSpec, TargetSpec


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueRef(_Strict):
    """A value supplied at invocation time or fixed in the artifact. Secrets are not expressible."""

    from_input: str | None = None
    literal: str | None = None
    vocab: str | None = None
    vocab_from_input: str | None = None  # the tenant's label for an enum input's value (e.g. money_market -> "Money Market")


class UrlMatches(_Strict):
    kind: Literal["url_matches"] = "url_matches"
    route: str
    frame_path: list[FrameRef] = Field(default_factory=list)
    params_equal: dict[str, ValueRef] = Field(default_factory=dict)


class ElementPresent(_Strict):
    kind: Literal["element_present"] = "element_present"
    target: TargetSpec


class ElementAbsent(_Strict):
    kind: Literal["element_absent"] = "element_absent"
    target: TargetSpec


class TextInRegion(_Strict):
    """Regex ``pattern`` (case-insensitive) or ``equals`` a value (whitespace-normalized contains)."""

    kind: Literal["text_in_region"] = "text_in_region"
    region: RegionSpec = Field(default_factory=RegionSpec)
    pattern: str | None = None
    contains: ValueRef | None = None


class FrameLoaded(_Strict):
    kind: Literal["frame_loaded"] = "frame_loaded"
    frame_path: list[FrameRef]
    route: str | None = None


class DialogPresent(_Strict):
    kind: Literal["dialog_present"] = "dialog_present"
    dialog_type: Literal["alert", "confirm", "prompt", "beforeunload", "any"] = "any"
    text_pattern: str


class HttpStatusIs(_Strict):
    """Last main-document (or frame document) response status in the given class, e.g. 5xx."""

    kind: Literal["http_status"] = "http_status"
    frame_path: list[FrameRef] = Field(default_factory=list)
    status_class: Literal["4xx", "5xx"]


class AllOf(_Strict):
    kind: Literal["all_of"] = "all_of"
    conditions: list[Condition]


class AnyOf(_Strict):
    kind: Literal["any_of"] = "any_of"
    conditions: list[Condition]


class Not(_Strict):
    kind: Literal["not"] = "not"
    condition: Condition


Condition = Annotated[
    UrlMatches | ElementPresent | ElementAbsent | TextInRegion | FrameLoaded | DialogPresent | HttpStatusIs | AllOf | AnyOf | Not,
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


class ConditionResult(_Strict):
    holds: bool
    detail: str = ""
