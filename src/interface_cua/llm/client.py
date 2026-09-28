"""LLM planner contract. The model returns one validated ``DiscoveryDecision`` per turn through a
strict tool schema. It can pick controls by handle, name declared inputs, and propose checks; it
cannot express selectors, URLs, code, or concrete sensitive values.

Never imported by ``interface_cua.replay`` (enforced by a test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from interface_cua.domain.types import OutputType

DecisionAction = Literal["click", "type", "select", "press", "navigate", "extract", "wait", "done", "give_up"]


class CheckProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label_text: str = Field(description="Visible label next to the value, e.g. 'Member ID'")
    frame_index: int = 0
    equals_input: str | None = Field(default=None, description="Declared input the value must equal")
    equals_text: str | None = Field(default=None, description="Literal non-sensitive text the value must equal")


class DiscoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    handle: str | None = None
    input_name: str | None = None
    literal_value: str | None = None
    key: Literal["Enter", "Tab", "Escape"] | None = None
    route_handle: str | None = None
    output_name: str | None = None
    output_type: OutputType | None = None
    label_text: str | None = None
    frame_index: int | None = None
    intent_risk: Literal["read_only", "reversible", "irreversible"] = "reversible"
    expected_effect: str = ""
    rationale: str = Field(default="", max_length=400)
    checkpoint: list[CheckProposal] | None = None


def decision_tool_schema() -> dict[str, object]:
    """Strict JSON schema for the single ``act`` tool (all keys required; optional ones nullable)."""

    def nullable(schema: dict[str, object]) -> dict[str, object]:
        t = schema.get("type")
        return {**schema, "type": [t, "null"]} if isinstance(t, str) else schema

    check = {
        "type": "object",
        "properties": {
            "label_text": {"type": "string"},
            "frame_index": {"type": "integer"},
            "equals_input": {"type": ["string", "null"]},
            "equals_text": {"type": ["string", "null"]},
        },
        "required": ["label_text", "frame_index", "equals_input", "equals_text"],
        "additionalProperties": False,
    }
    props: dict[str, object] = {
        "action": {"type": "string", "enum": list(DecisionAction.__args__)},  # type: ignore[attr-defined]
        "handle": nullable({"type": "string", "description": "Number of the control from the inventory/screenshot."}),
        "input_name": nullable({"type": "string", "description": "Declared input whose value to type/select."}),
        "literal_value": nullable({"type": "string", "description": "Non-sensitive literal (e.g. an option label)."}),
        "key": {"type": ["string", "null"], "enum": ["Enter", "Tab", "Escape", None]},
        "route_handle": nullable({"type": "string"}),
        "output_name": nullable({"type": "string", "description": "snake_case name of a value to extract."}),
        "output_type": {"type": ["string", "null"], "enum": [*[t.value for t in OutputType], None]},
        "label_text": nullable({"type": "string", "description": "For extract: the visible label next to the value."}),
        "frame_index": nullable({"type": "integer"}),
        "intent_risk": {"type": "string", "enum": ["read_only", "reversible", "irreversible"]},
        "expected_effect": {"type": "string"},
        "rationale": {"type": "string", "description": "One short sentence. No sensitive values."},
        "checkpoint": {"type": ["array", "null"], "items": check},
    }
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def portable_tool_schema() -> dict[str, object]:
    """Same decision contract for providers whose function-calling accepts only a JSON Schema subset
    (e.g. Gemini via its OpenAI-compatible endpoint): plain types, optional keys instead of nullable
    unions. Every response is still validated against ``DiscoveryDecision`` before it is used."""
    strict = decision_tool_schema()
    props: dict[str, object] = {}
    for name, spec in strict["properties"].items():  # type: ignore[attr-defined]
        spec = dict(spec)
        t = spec.get("type")
        if isinstance(t, list):
            spec["type"] = next(x for x in t if x != "null")
        if "enum" in spec:
            spec["enum"] = [v for v in spec["enum"] if v is not None]
        if name == "checkpoint":
            item = dict(spec["items"])
            item["properties"] = {
                k: {"type": next(x for x in v["type"] if x != "null") if isinstance(v["type"], list) else v["type"]}
                for k, v in item["properties"].items()
            }
            item["required"] = ["label_text"]
            item.pop("additionalProperties", None)
            spec["items"] = item
        props[name] = spec
    return {"type": "object", "properties": props, "required": ["action", "intent_risk", "rationale"]}


def coerce_decision(raw: dict[str, Any]) -> DiscoveryDecision:
    """Lenient front door for non-strict providers: drop empty optionals and unknown keys (they can
    never be acted on), then validate strictly."""
    known = set(DiscoveryDecision.model_fields)
    cleaned: dict[str, Any] = {k: v for k, v in raw.items() if k in known and v not in (None, "", [])}
    if isinstance(cleaned.get("handle"), int | float):
        cleaned["handle"] = str(int(cleaned["handle"]))
    if isinstance(cleaned.get("checkpoint"), list):
        cleaned["checkpoint"] = [{k: v for k, v in c.items() if v not in (None, "")} for c in cleaned["checkpoint"] if isinstance(c, dict)]
    return DiscoveryDecision.model_validate(cleaned)


@dataclass
class DeclaredInput:
    name: str
    type: str
    description: str
    sensitivity: str
    enum: list[str] | None = None


@dataclass
class DiscoveryContext:
    goal: str
    inputs: list[DeclaredInput]
    route_handles: list[str]
    observation_text: str  # already masked for egress
    screenshot_png: bytes  # already masked, with set-of-marks
    history: list[str]
    feedback: str | None
    step: int
    max_steps: int
    extracted: list[str] = field(default_factory=list)
    outputs: list[tuple[str, str]] = field(default_factory=list)  # required (name, type) to extract


@dataclass
class DecisionResult:
    decision: DiscoveryDecision | None
    error: str | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str | None = None


class LLMClient(Protocol):
    model: str
    prompt_template_hash: str

    async def decide(self, ctx: DiscoveryContext) -> DecisionResult: ...
