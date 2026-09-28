"""Human-readable review summary rendered next to every artifact version."""

from __future__ import annotations

from typing import Any

from interface_cua.domain.artifacts import CapabilityArtifact
from interface_cua.domain.conditions import (
    AllOf,
    AnyOf,
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
from interface_cua.domain.targets import TargetSpec


def _ref(v: ValueRef | None) -> str:
    if v is None:
        return "-"
    if v.from_input:
        return f"input `{v.from_input}`"
    if v.vocab:
        return f"vocabulary `{v.vocab}`"
    if v.vocab_from_input:
        return f"tenant label of input `{v.vocab_from_input}`"
    return f"literal '{v.literal}'"


def cond_text(c: Any) -> str:
    if c is None:
        return "-"
    if isinstance(c, UrlMatches):
        extra = "".join(f", {k} = {_ref(v)}" for k, v in c.params_equal.items())
        return f"URL matches `{c.route}`{extra}"
    if isinstance(c, ElementPresent):
        return f"'{c.target.description}' present"
    if isinstance(c, ElementAbsent):
        return f"'{c.target.description}' absent"
    if isinstance(c, TextInRegion):
        where = c.region.target.description if c.region.target else ("any frame" if c.region.any_frame else "page")
        what = f"/{c.pattern}/" if c.pattern else _ref(c.contains)
        return f"{where} contains {what}"
    if isinstance(c, FrameLoaded):
        return f"frame {'/'.join(f.name or f.route or '?' for f in c.frame_path)} at `{c.route}`"
    if isinstance(c, DialogPresent):
        return f"{c.dialog_type} dialog /{c.text_pattern}/"
    if isinstance(c, HttpStatusIs):
        return f"HTTP {c.status_class}"
    if isinstance(c, AllOf):
        return " AND ".join(cond_text(x) for x in c.conditions)
    if isinstance(c, AnyOf):
        return " OR ".join(cond_text(x) for x in c.conditions)
    if isinstance(c, Not):
        return f"NOT ({cond_text(c.condition)})"
    return str(c)


def _target(t: TargetSpec | None) -> str:
    if t is None:
        return "-"
    parts = []
    for c in t.ordered():
        d = c.model_dump(exclude={"adapter_kinds", "expected_count", "rationale", "score", "strategy"}, exclude_none=True)
        parts.append(f"{c.strategy}({', '.join(f'{k}={v}' for k, v in d.items())}) [{c.score}]")
    frame = "/".join(f.name or f.route or "?" for f in t.frame_path)
    return (f"in frame `{frame}`: " if frame else "") + " > ".join(parts)


def render_review(a: CapabilityArtifact) -> str:
    L: list[str] = []
    L.append(f"# Capability `{a.ref}` - {a.capability.name}")
    L.append("")
    L.append(f"- **What it does:** {a.capability.description}")
    L.append(f"- **Status:** {a.lifecycle.status}  |  **Content hash:** `{a.content_hash}`")
    L.append(
        f"- **Target:** {a.target.vendor_product} ({a.target.surface_type}), app profile `{a.compatibility.app_profile}`, "
        f"product versions `{a.compatibility.version_range}`, entry `{a.target.entry_point.route_handle}` (`{a.target.entry_point.route}`)"
    )
    L.append(f"- **Side effects:** {a.contract.side_effects}")
    L.append(
        f"- **Provenance:** run `{a.provenance.source_run_id}`, model `{a.provenance.model}`, "
        f"prompt `{a.provenance.prompt_template_hash}`, "
        f"{a.provenance.executed_actions} executed / {a.provenance.dropped_actions} dropped actions"
        + (f", parent `{a.provenance.parent}`, patches {a.provenance.authored_patches}" if a.provenance.parent else "")
    )
    L.append("")
    L.append("## Contract (what a calling agent supplies and gets back)")
    L.append("")
    L.append("| Input | Type | Constraint | Sensitivity |")
    L.append("|---|---|---|---|")
    for name, p in a.contract.inputs.get("properties", {}).items():
        L.append(f"| `{name}` | {p.get('type')} | {p.get('pattern') or p.get('enum') or '-'} | {p.get('x-sensitivity')} |")
    L.append("")
    L.append("| Output | Type | Sensitivity |")
    L.append("|---|---|---|")
    for name, p in a.contract.outputs.get("properties", {}).items():
        L.append(f"| `{name}` | {p.get('x-cua-type') or p.get('type')} | {p.get('x-sensitivity')} |")
    L.append("")
    if a.contract.business_outcomes:
        L.append("Business outcomes (legitimate answers, not errors): " + ", ".join(f"`{o.code}`" for o in a.contract.business_outcomes))
        L.append("")
    L.append("## Steps")
    L.append("")
    L.append("| # | Action | Target (locator candidates, best first) | Value | Risk | Idempotent | Postcondition | Scoped states |")
    L.append("|---|---|---|---|---|---|---|---|")
    for s in a.steps:
        states = ", ".join(f"{m.state}->{m.outcome_code or m.on}" for m in s.states) or "-"
        flag = " **(review)**" if s.review_required else ""
        L.append(
            f"| {s.id}{flag} | {s.action.value}: {s.description} | {_target(s.target)} | {_ref(s.value)} | {s.risk_class.value} | "
            f"{'yes' if s.idempotent else 'NO'} | {cond_text(s.postcondition)} | {states} |"
        )
    L.append("")
    L.append("## Outputs are read by")
    L.append("")
    for e in a.extract:
        L.append(f"- `{e.output}` ({e.type.value}) after `{e.after_step}` from {_target(e.target)}")
    L.append("")
    L.append("## Success condition (all must hold)")
    L.append("")
    for c in a.success.checks:
        L.append(f"- {cond_text(c)}")
    L.append("")
    L.append("## Policy (narrowing only)")
    L.append("")
    L.append(f"- Actions: {', '.join(x.value for x in a.policy.allowed_actions)}")
    L.append(f"- Routes: {', '.join(f'`{r}`' for r in a.policy.allowed_routes)}")
    L.append("")
    L.append("## Needs reviewer attention")
    L.append("")
    L.extend(f"- {i}" for i in a.review.items) if a.review.items else L.append("- nothing flagged")
    L.append("")
    L.append("## Lifecycle")
    L.append("")
    for t in a.lifecycle.transitions:
        L.append(
            f"- {t.at} -> **{t.to}** by {t.actor}"
            + (f": {t.note}" if t.note else "")
            + (f" (runs {', '.join(t.run_ids)})" if t.run_ids else "")
        )
    return "\n".join(L) + "\n"
